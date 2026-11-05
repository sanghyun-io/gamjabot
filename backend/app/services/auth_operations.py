"""Durable, owner-scoped state for Core provider-auth operations."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal, Mapping

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.schemas.provider_auth import (
    AuthOperationIntent, ConfirmedRevokeOutcome, DurableOperation, NonterminalPhase,
    OperationBinding, OperationPhase, OperationState, TerminalOutcome,
)


_OPERATION_PHASE = TypeAdapter(OperationPhase)
_OPERATION_STATE = TypeAdapter(OperationState)
_NONTERMINAL_PHASE = TypeAdapter(NonterminalPhase)
_CONFIRMED_REVOKE_OUTCOME = TypeAdapter(ConfirmedRevokeOutcome)
_SELECTOR_FIELDS = frozenset({
    "operation_id", "owner_id", "connection_id", "kind", "intent_fingerprint",
    "before_state_fingerprint", "revoke_requested",
})
_KINDS = frozenset({"auth", "disconnect", "grant_replace", "same_grant_refresh"})


class AuthOperationError(Exception):
    """A finite, safe auth-operation failure for later API mapping."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GenerationAdoption:
    """Closed CAS result; ``already_current`` carries no predecessor proof."""

    state: Literal["adopted", "already_current"]
    operation: DurableOperation


_SELECT = """SELECT operation_id,owner_id,connection_id,kind,state,phase,
    idempotency_key_hash,server_replay_discriminator_hash,intent_fingerprint,
    before_state_fingerprint,current_generation_fingerprint,revoke_requested,
    revoke_attempt_started_at,revoke_state,attempt_count,next_attempt_at,error_code,
    created_at,updated_at,expires_at,terminal_at
    FROM connection_auth_operations"""


def _hash(value: bytes) -> str:
    return sha256(value).hexdigest()


def _hex64(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _positive_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _operation(row, *, replayed: bool = False) -> DurableOperation:
    if row is None:
        raise AuthOperationError("configuration_changed")
    return DurableOperation.model_validate({**dict(row), "replayed": replayed})


def _binding_where() -> str:
    return """operation_id=:operation_id AND owner_id=:owner_id
        AND connection_id=:connection_id AND intent_fingerprint=:intent_fingerprint
        AND current_generation_fingerprint=:current_generation_fingerprint"""


def _binding_values(binding: OperationBinding) -> dict:
    return binding.model_dump(mode="python")


def _begin_immediate(db: Session) -> None:
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))


def _get(db: Session, operation_id: str, *, replayed: bool = False) -> DurableOperation:
    row = db.execute(text(f"{_SELECT} WHERE operation_id=:operation_id"),
                     {"operation_id": operation_id}).mappings().first()
    return _operation(row, replayed=replayed)


def operation(db: Session, owner_id: int, operation_id: str) -> DurableOperation:
    """Read one operation only through its exact public owner binding."""
    if not _positive_integer(owner_id) or not _hex64(operation_id):
        raise AuthOperationError("configuration_changed")
    row = db.execute(text(
        f"{_SELECT} WHERE operation_id=:operation_id AND owner_id=:owner_id"
    ), {"operation_id": operation_id, "owner_id": owner_id}).mappings().first()
    return _operation(row)


def _selector_values(selector: Mapping) -> dict:
    if not isinstance(selector, Mapping) or set(selector) != _SELECTOR_FIELDS:
        raise AuthOperationError("configuration_changed")
    values = dict(selector)
    if (not _hex64(values["operation_id"])
            or not _positive_integer(values["owner_id"])
            or not _positive_integer(values["connection_id"])
            or not isinstance(values["kind"], str)
            or values["kind"] not in _KINDS
            or not _hex64(values["intent_fingerprint"])
            or not _hex64(values["before_state_fingerprint"])
            or type(values["revoke_requested"]) is not bool):
        raise AuthOperationError("configuration_changed")
    return values


def recovery_selector(value: DurableOperation) -> dict:
    """Project the seven non-secret fields accepted by Core recovery inspect."""
    return _selector_values({
        "operation_id": value.operation_id,
        "owner_id": value.owner_id,
        "connection_id": value.connection_id,
        "kind": value.kind,
        "intent_fingerprint": value.intent_fingerprint,
        "before_state_fingerprint": value.before_state_fingerprint,
        "revoke_requested": value.revoke_requested,
    })


def _selector_where() -> str:
    return """operation_id=:operation_id AND owner_id=:owner_id
        AND connection_id=:connection_id AND kind=:kind
        AND intent_fingerprint=:intent_fingerprint
        AND before_state_fingerprint=:before_state_fingerprint
        AND revoke_requested=:revoke_requested"""


def _selector_matches(row, selector: dict) -> bool:
    return row is not None and all(row[field] == value for field, value in selector.items())


def adopt_generation(db: Session, *, selector: Mapping, expected_generation: str,
                     new_generation: str) -> GenerationAdoption:
    """CAS one full safe selector to the current Core generation.

    ``adopted`` proves this call performed the expected-to-new transition.
    ``already_current`` is only a recoverable observation after a zero-row CAS;
    callers must take no action until a fresh DB reread and full Core secure
    inspection agree on the current generation and binding.
    """
    values = _selector_values(selector)
    if (not _hex64(expected_generation) or not _hex64(new_generation)
            or expected_generation == new_generation):
        raise AuthOperationError("configuration_changed")
    _begin_immediate(db)
    try:
        changed = db.execute(text(f"""UPDATE connection_auth_operations
            SET current_generation_fingerprint=:new_generation,
                updated_at=CURRENT_TIMESTAMP
            WHERE {_selector_where()}
                AND current_generation_fingerprint=:expected_generation
                AND terminal_at IS NULL"""), {
                    **values, "expected_generation": expected_generation,
                    "new_generation": new_generation,
                }).rowcount
        if changed == 1:
            adopted = _get(db, values["operation_id"])
            db.commit()
            return GenerationAdoption(state="adopted", operation=adopted)
        current = db.execute(text(
            f"{_SELECT} WHERE {_selector_where()}"
        ), values).mappings().first()
        if (current is None or current["terminal_at"] is not None
                or current["current_generation_fingerprint"] != new_generation):
            raise AuthOperationError("configuration_changed")
        db.commit()
        return GenerationAdoption(
            state="already_current", operation=_operation(current))
    except AuthOperationError:
        db.rollback()
        raise


def _page_values(owner_id: int, after_operation_id: str | None, limit: int) -> dict:
    if (not _positive_integer(owner_id) or type(limit) is not int or not 1 <= limit <= 100
            or (after_operation_id is not None and not _hex64(after_operation_id))):
        raise AuthOperationError("configuration_changed")
    return {
        "owner_id": owner_id,
        "after_operation_id": after_operation_id or "",
        "limit": limit,
    }


def nonterminal_candidates(db: Session, owner_id: int, *,
                           after_operation_id: str | None = None,
                           limit: int = 25) -> list[DurableOperation]:
    """Return one stable owner-scoped recovery page without touching credentials."""
    values = _page_values(owner_id, after_operation_id, limit)
    rows = db.execute(text(f"""{_SELECT}
        WHERE owner_id=:owner_id AND operation_id>:after_operation_id
            AND (terminal_at IS NULL OR phase!='terminal')
        ORDER BY operation_id LIMIT :limit"""), values).mappings().all()
    return [_operation(row) for row in rows]


def terminal_receipt_candidates(db: Session, owner_id: int, *, terminal_before: datetime,
                                after_operation_id: str | None = None,
                                limit: int = 25) -> list[DurableOperation]:
    """Return a bounded terminal page for receipt-only acknowledgment."""
    if not isinstance(terminal_before, datetime) or terminal_before.tzinfo is None:
        raise AuthOperationError("configuration_changed")
    values = _page_values(owner_id, after_operation_id, limit)
    values["terminal_before"] = terminal_before.astimezone(timezone.utc).replace(tzinfo=None)
    rows = db.execute(text(f"""{_SELECT}
        WHERE owner_id=:owner_id AND operation_id>:after_operation_id
            AND phase='terminal' AND terminal_at IS NOT NULL
            AND terminal_at<=:terminal_before
        ORDER BY operation_id LIMIT :limit"""), values).mappings().all()
    return [_operation(row) for row in rows]


def begin_operation(db: Session, intent: AuthOperationIntent,
                    idempotency_key: str | None) -> DurableOperation:
    """Commit a safe operation row before any credential mutation."""
    if idempotency_key is not None and (
            not 1 <= len(idempotency_key) <= 200
            or any(not character.isprintable() for character in idempotency_key)):
        raise AuthOperationError("idempotency_conflict")
    key_hash = _hash(idempotency_key.encode()) if idempotency_key is not None else None
    _begin_immediate(db)
    try:
        if key_hash is not None:
            prior = db.execute(text(
                f"{_SELECT} WHERE owner_id=:owner_id AND idempotency_key_hash=:key_hash"
            ), {"owner_id": intent.owner_id, "key_hash": key_hash}).mappings().first()
            if prior is not None:
                if prior["intent_fingerprint"] != intent.intent_fingerprint:
                    raise AuthOperationError("idempotency_conflict")
                db.commit()
                return _operation(prior, replayed=True)

        active = db.execute(text(
            f"{_SELECT} WHERE owner_id=:owner_id AND connection_id=:connection_id "
            "AND (terminal_at IS NULL OR phase != 'terminal') ORDER BY created_at LIMIT 1"
        ), {"owner_id": intent.owner_id,
            "connection_id": intent.connection_id}).mappings().first()
        if active is not None:
            if idempotency_key is None:
                db.commit()
                return _operation(active, replayed=True)
            raise AuthOperationError("auth_operation_busy")

        discriminator_hash = (
            None if key_hash is not None else _hash(bytes.fromhex(intent.operation_id)))
        db.execute(text("""INSERT INTO connection_auth_operations (
            operation_id,owner_id,connection_id,kind,state,phase,idempotency_key_hash,
            server_replay_discriminator_hash,intent_fingerprint,before_state_fingerprint,
            current_generation_fingerprint,revoke_requested,revoke_state,expires_at
        ) VALUES (
            :operation_id,:owner_id,:connection_id,:kind,'prepared','none',:key_hash,
            :discriminator_hash,:intent_fingerprint,:before_state_fingerprint,
            :current_generation_fingerprint,:revoke_requested,:revoke_state,:expires_at
        )"""), {
            **intent.model_dump(mode="python"),
            "key_hash": key_hash,
            "discriminator_hash": discriminator_hash,
            "revoke_state": "pending" if intent.revoke_requested else "skipped",
        })
        db.commit()
        return _get(db, intent.operation_id)
    except AuthOperationError:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise AuthOperationError("configuration_changed") from error


def auth_start_preflight(db: Session, *, owner_id: int, connection_id: int,
                         intent_fingerprint: str,
                         idempotency_key: str) -> DurableOperation | None:
    """Resolve exact keyed replay/conflict and busy before any Core call."""
    if (not _positive_integer(owner_id) or not _positive_integer(connection_id)
            or not _hex64(intent_fingerprint)
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 200
            or any(not character.isprintable() for character in idempotency_key)):
        raise AuthOperationError("idempotency_conflict")
    key_hash = _hash(idempotency_key.encode())
    prior = db.execute(text(
        f"{_SELECT} WHERE owner_id=:owner_id AND idempotency_key_hash=:key_hash"
    ), {"owner_id": owner_id, "key_hash": key_hash}).mappings().first()
    if prior is not None:
        if (prior["connection_id"] != connection_id
                or prior["kind"] not in {"auth", "grant_replace", "same_grant_refresh"}
                or prior["intent_fingerprint"] != intent_fingerprint):
            raise AuthOperationError("idempotency_conflict")
        return _operation(prior, replayed=True)
    active = blocking_operation(db, owner_id, connection_id)
    if active is not None:
        raise AuthOperationError("auth_operation_busy")
    return None


def record_auth_state(db: Session, binding: OperationBinding,
                      state: OperationState) -> DurableOperation:
    """Persist one closed nonterminal Core auth state under the exact binding."""
    try:
        state = _OPERATION_STATE.validate_python(state)
    except ValidationError as error:
        raise AuthOperationError("configuration_changed") from error
    if state in {"succeeded", "cancelled", "expired", "interrupted", "failed"}:
        raise AuthOperationError("configuration_changed")
    _begin_immediate(db)
    try:
        result = db.execute(text(f"""UPDATE connection_auth_operations
            SET state=:state,updated_at=CURRENT_TIMESTAMP
            WHERE {_binding_where()} AND terminal_at IS NULL"""), {
                **_binding_values(binding), "state": state,
            })
        if result.rowcount != 1:
            raise AuthOperationError("configuration_changed")
        db.commit()
        return _get(db, binding.operation_id)
    except AuthOperationError:
        db.rollback()
        raise


def record_auth_terminal_pending(db: Session, binding: OperationBinding, *,
                                 state: OperationState,
                                 error_code: str) -> DurableOperation:
    """Record Core terminal intent; cleanup proof still owns terminal phase."""
    try:
        state = _OPERATION_STATE.validate_python(state)
    except ValidationError as error:
        raise AuthOperationError("configuration_changed") from error
    if state not in {"cancelled", "expired", "interrupted", "failed"}:
        raise AuthOperationError("configuration_changed")
    _begin_immediate(db)
    try:
        result = db.execute(text(f"""UPDATE connection_auth_operations
            SET state=:state,error_code=:error_code,updated_at=CURRENT_TIMESTAMP
            WHERE {_binding_where()} AND terminal_at IS NULL"""), {
                **_binding_values(binding), "state": state,
                "error_code": error_code,
            })
        if result.rowcount != 1:
            raise AuthOperationError("configuration_changed")
        db.commit()
        return _get(db, binding.operation_id)
    except AuthOperationError:
        db.rollback()
        raise


def advance_phase(db: Session, binding: OperationBinding, *, expected: OperationPhase,
                  target: NonterminalPhase) -> DurableOperation:
    """Advance one nonterminal phase using the full current-generation binding."""
    try:
        expected = _OPERATION_PHASE.validate_python(expected)
        target = _NONTERMINAL_PHASE.validate_python(target)
    except ValidationError as error:
        raise AuthOperationError("configuration_changed") from error
    _begin_immediate(db)
    try:
        updated = advance_phase_no_commit(
            db, binding, expected=expected, target=target)
        db.commit()
        return updated
    except AuthOperationError:
        db.rollback()
        raise


def advance_phase_no_commit(db: Session, binding: OperationBinding, *,
                            expected: OperationPhase,
                            target: NonterminalPhase) -> DurableOperation:
    """Apply an exact phase CAS inside a transaction owned by the caller."""
    try:
        expected = _OPERATION_PHASE.validate_python(expected)
        target = _NONTERMINAL_PHASE.validate_python(target)
    except ValidationError as error:
        raise AuthOperationError("configuration_changed") from error
    result = db.execute(text(f"""UPDATE connection_auth_operations
        SET phase=:target,updated_at=CURRENT_TIMESTAMP
        WHERE {_binding_where()} AND phase=:expected AND terminal_at IS NULL"""), {
            **_binding_values(binding), "expected": expected, "target": target,
        })
    if result.rowcount != 1:
        raise AuthOperationError("configuration_changed")
    return _get(db, binding.operation_id)


def blocking_operation(db: Session, owner_id: int,
                       connection_id: int) -> DurableOperation | None:
    """Return the operation that still owns resolver/busy cleanup responsibility."""
    row = db.execute(text(
        f"{_SELECT} WHERE owner_id=:owner_id AND connection_id=:connection_id "
        "AND (terminal_at IS NULL OR phase != 'terminal') ORDER BY created_at LIMIT 1"
    ), {"owner_id": owner_id, "connection_id": connection_id}).mappings().first()
    return None if row is None else _operation(row)


def mark_revoke_attempt_started(db: Session, binding: OperationBinding,
                                started_at: datetime) -> DurableOperation:
    """Set the remote-attempt marker once; a replay never moves it forward."""
    if started_at.tzinfo is None:
        raise ValueError("revoke attempt timestamp must be timezone-aware")
    _begin_immediate(db)
    try:
        result = db.execute(text(f"""UPDATE connection_auth_operations
            SET revoke_attempt_started_at=COALESCE(revoke_attempt_started_at,:started_at),
                updated_at=CURRENT_TIMESTAMP
            WHERE {_binding_where()} AND terminal_at IS NULL
                AND revoke_requested=1 AND revoke_state='pending'"""), {
                **_binding_values(binding), "started_at": started_at.isoformat(),
            })
        if result.rowcount != 1:
            raise AuthOperationError("configuration_changed")
        db.commit()
        return _get(db, binding.operation_id)
    except AuthOperationError:
        db.rollback()
        raise


def record_revoke_outcome(db: Session, binding: OperationBinding,
                          outcome: ConfirmedRevokeOutcome) -> DurableOperation:
    """Persist one closed result; a crash-unknown caller records conservative failure."""
    try:
        outcome = _CONFIRMED_REVOKE_OUTCOME.validate_python(outcome)
    except ValidationError as error:
        raise AuthOperationError("configuration_changed") from error
    _begin_immediate(db)
    try:
        current = db.execute(text(
            f"{_SELECT} WHERE {_binding_where()}"
        ), _binding_values(binding)).mappings().first()
        if current is None or current["terminal_at"] is not None:
            raise AuthOperationError("configuration_changed")
        if current["revoke_state"] == outcome:
            db.commit()
            return _operation(current, replayed=True)
        if (not current["revoke_requested"] or current["revoke_state"] != "pending"
                or current["revoke_attempt_started_at"] is None):
            raise AuthOperationError("configuration_changed")
        result = db.execute(text(f"""UPDATE connection_auth_operations
            SET revoke_state=:outcome,updated_at=CURRENT_TIMESTAMP
            WHERE {_binding_where()} AND terminal_at IS NULL
                AND revoke_requested=1 AND revoke_attempt_started_at IS NOT NULL
                AND revoke_state='pending'"""), {
                **_binding_values(binding), "outcome": outcome,
            })
        if result.rowcount != 1:
            raise AuthOperationError("configuration_changed")
        db.commit()
        return _get(db, binding.operation_id)
    except AuthOperationError:
        db.rollback()
        raise


def terminalize(db: Session, binding: OperationBinding,
                outcome: TerminalOutcome, *, selector: Mapping | None = None) -> DurableOperation:
    """Record terminal phase and timestamp together after caller-confirmed cleanup."""
    _begin_immediate(db)
    try:
        current = db.execute(text(
            f"{_SELECT} WHERE {_binding_where()}"
        ), _binding_values(binding)).mappings().first()
        if current is None:
            raise AuthOperationError("configuration_changed")
        selected = _selector_values(selector) if selector is not None else None
        if selected is not None and not _selector_matches(current, selected):
            raise AuthOperationError("configuration_changed")
        if current["terminal_at"] is not None:
            if (selected is None or current["phase"] != "terminal"
                    or current["state"] != outcome.state
                    or current["error_code"] != outcome.error_code
                    or current["revoke_state"] != outcome.revoke_state):
                raise AuthOperationError("configuration_changed")
            db.commit()
            return _operation(current, replayed=True)
        if current["revoke_requested"]:
            attempt_started = current["revoke_attempt_started_at"] is not None
            if attempt_started and current["revoke_state"] == "pending":
                raise AuthOperationError("configuration_changed")
            if attempt_started and (
                    outcome.state != "succeeded"
                    or current["revoke_state"] != outcome.revoke_state):
                raise AuthOperationError("configuration_changed")
            if not attempt_started and outcome.state == "succeeded":
                raise AuthOperationError("configuration_changed")
        elif outcome.revoke_state != "skipped":
            raise AuthOperationError("configuration_changed")
        result = db.execute(text(f"""UPDATE connection_auth_operations
            SET state=:state,phase='terminal',terminal_at=CURRENT_TIMESTAMP,
                error_code=:error_code,revoke_state=:revoke_state,
                updated_at=CURRENT_TIMESTAMP
            WHERE {_binding_where()} AND terminal_at IS NULL"""), {
                **_binding_values(binding), **outcome.model_dump(mode="python"),
            })
        if result.rowcount != 1:
            raise AuthOperationError("configuration_changed")
        db.commit()
        return _get(db, binding.operation_id)
    except AuthOperationError:
        db.rollback()
        raise
