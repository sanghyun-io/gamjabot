"""Pure readiness metadata and normalized explicit connection-test history."""

import hashlib
import json
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.collaboration import ConnectionTestHistory, ProviderConnection
from app.models.models import Agent
from app.services import provider_catalog


CAPABILITIES = ('max_tokens', 'temperature', 'tools', 'vision')
OUTCOME_CODES = frozenset({
    'connected', 'missing_secret', 'missing_model', 'secret_store_unavailable',
    'invalid_credential', 'unreachable', 'redirect_refused', 'provider_error',
    'rate_limited', 'quota_exhausted', 'invalid_response', 'connection_disabled',
    'credential_locked', 'configuration_changed',
})
MAX_HISTORY = 20
MAX_BINDINGS = 50


def safe_base_url(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None or parsed.password is not None:
        return None
    authority = host if ':' not in host else f'[{host}]'
    if port is not None:
        authority += f':{port}'
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))


def declared_capabilities(value: dict | None) -> dict[str, bool]:
    declared = (value or {}).get('declared', {})
    return {name: bool(declared[name]) for name in CAPABILITIES if name in declared}


def public_config(row: ProviderConnection) -> dict:
    return {
        'base_url': safe_base_url(row.base_url),
        'default_model_id': row.default_model_id or '',
        'declared_capabilities': declared_capabilities(row.capabilities),
    }


def test_fingerprint(row: ProviderConnection, model_id: str, profile: str) -> str:
    if row.provider_id is None:
        normalized = provider_catalog.normalize_legacy_connection(row)
        provider_id = normalized.provider_id
        protocol_id = normalized.protocol_id
        auth_method_id = normalized.auth_method_id
        public_config_version = normalized.public_config_version
        public_fields = normalized.public_config
        auth_grant_revision = normalized.auth_grant_revision
        credential_state = normalized.credential_state
    else:
        provider_id = row.provider_id
        protocol_id = row.protocol_id
        auth_method_id = row.auth_method_id
        public_config_version = row.public_config_version
        public_fields = row.public_config or {}
        auth_grant_revision = int(row.auth_grant_revision or 0)
        credential_state = row.credential_state
    projection = {
        'provider_kind': row.kind,
        'provider_id': provider_id,
        'protocol_id': protocol_id,
        'auth_method_id': auth_method_id,
        'public_config_version': public_config_version,
        'public_config': public_fields,
        'auth_grant_revision': auth_grant_revision,
        'credential_state': credential_state,
        'model_id': model_id,
        'profile': profile,
        'enabled': bool(row.enabled),
        'auth_required': bool(row.auth_required),
        'has_secret': bool(row.secret_ref),
        'readiness_revision': int(row.readiness_revision or 1),
        **public_config(row),
    }
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def config_fingerprint(row: ProviderConnection) -> str:
    """Compatibility projection for callers that plan the default full Test."""
    return test_fingerprint(row, row.default_model_id or '', 'full')


def credential_observation(code: str, auth_required: bool) -> str:
    if not auth_required:
        return 'not_required'
    return {
        'connected': 'verified',
        'invalid_credential': 'rejected',
        'missing_secret': 'missing',
        'credential_locked': 'locked',
        'secret_store_unavailable': 'unavailable',
    }.get(code, 'unknown')


def _verified(value: dict | None) -> dict[str, bool] | None:
    if value is None:
        return None
    return {name: bool(value.get(name, False)) for name in CAPABILITIES}


def record_public(row: ConnectionTestHistory, current_fingerprint: str) -> dict:
    return {
        'id': row.id,
        'connection_id': row.connection_id,
        'tested_at': row.tested_at,
        'duration_ms': row.duration_ms,
        'model_id': row.model_id,
        'ok': bool(row.ok),
        'code': row.outcome_code,
        'config_fingerprint': row.config_fingerprint,
        'config_current': row.config_fingerprint == current_fingerprint,
        'credential_observation': row.credential_observation,
        'verified_capabilities': _verified(row.verified_capabilities),
        'profile': row.profile,
        'actual_request_count': row.actual_request_count,
    }


def record_test(db: Session, *, owner_id: int, connection_id: int, config_revision: int,
                fingerprint: str, model_id: str | None, ok: bool, code: str,
                auth_required: bool, verified: dict | None, duration_ms: int,
                profile: str, actual_request_count: int | None) -> dict | None:
    """Record only finite facts. A concurrently deleted connection has no durable history."""
    if code not in OUTCOME_CODES:
        code = 'provider_error'
        ok = False
    current = db.query(ProviderConnection).filter_by(id=connection_id, user_id=owner_id).first()
    if current is None:
        db.rollback()
        return None
    row = ConnectionTestHistory(
        owner_id=owner_id, connection_id=connection_id, config_revision=config_revision,
        config_fingerprint=fingerprint, model_id=model_id, ok=ok, outcome_code=code,
        credential_observation=credential_observation(code, auth_required),
        verified_capabilities=_verified(verified) if ok else None,
        duration_ms=max(0, min(int(duration_ms), 300_000)),
        profile=profile,
        actual_request_count=(None if actual_request_count is None
                              else max(0, int(actual_request_count))),
    )
    db.add(row)
    db.flush()
    old_ids = [item[0] for item in db.query(ConnectionTestHistory.id).filter_by(
        owner_id=owner_id, connection_id=connection_id).order_by(
            ConnectionTestHistory.tested_at.desc(), ConnectionTestHistory.id.desc()).offset(MAX_HISTORY).all()]
    if old_ids:
        db.execute(delete(ConnectionTestHistory).where(ConnectionTestHistory.id.in_(old_ids)))
    db.commit()
    db.refresh(row)
    current = db.query(ProviderConnection).filter_by(id=connection_id, user_id=owner_id).one()
    return record_public(row, test_fingerprint(current, row.model_id or '', row.profile or 'full'))


def history(db: Session, connection: ProviderConnection, limit: int) -> dict:
    fingerprint = config_fingerprint(connection)
    rows = db.query(ConnectionTestHistory).filter_by(
        owner_id=connection.user_id, connection_id=connection.id).order_by(
            ConnectionTestHistory.tested_at.desc(), ConnectionTestHistory.id.desc()).limit(limit).all()
    return {'connection_id': connection.id, 'current_config_fingerprint': fingerprint,
            'items': [record_public(row, test_fingerprint(
                connection, row.model_id or '', row.profile or 'full')) for row in rows]}


def overview(db: Session, owner_id: int) -> dict:
    connections = db.query(ProviderConnection).filter_by(user_id=owner_id).order_by(ProviderConnection.id).all()
    agents = db.query(Agent).filter_by(user_id=owner_id).order_by(
        Agent.connection_id, Agent.is_active.desc(), Agent.id).all()
    histories = db.query(ConnectionTestHistory).filter_by(owner_id=owner_id).order_by(
        ConnectionTestHistory.connection_id, ConnectionTestHistory.tested_at.desc(),
        ConnectionTestHistory.id.desc()).all()
    agents_by_connection: dict[int, list[Agent]] = defaultdict(list)
    history_by_connection: dict[int, list[ConnectionTestHistory]] = defaultdict(list)
    for agent in agents:
        if agent.connection_id is not None:
            agents_by_connection[agent.connection_id].append(agent)
    for row in histories:
        history_by_connection[row.connection_id].append(row)
    result = []
    for connection in connections:
        fingerprint = config_fingerprint(connection)
        current_history = [row for row in history_by_connection[connection.id]
                           if row.config_fingerprint == test_fingerprint(
                               connection, row.model_id or '', row.profile or 'full')]
        latest = history_by_connection[connection.id][0] if history_by_connection[connection.id] else None
        by_model: dict[str, ConnectionTestHistory] = {}
        for row in current_history:
            if row.model_id and row.model_id not in by_model:
                by_model[row.model_id] = row
        bindings, counts = [], {'active_bound': 0, 'active_missing_model': 0, 'inactive': 0, 'total': 0}
        rows = agents_by_connection[connection.id]
        for agent in rows[:MAX_BINDINGS]:
            active = bool(agent.is_active)
            model_id = agent.model_id or None
            status = 'inactive' if not active else 'bound' if model_id else 'model_missing'
            counts['total'] += 1
            counts['inactive' if not active else 'active_bound' if model_id else 'active_missing_model'] += 1
            observation_row = by_model.get(model_id) if active and model_id else None
            bindings.append({
                'agent_id': agent.id, 'agent_name': agent.name, 'agent_active': active,
                'model_id': model_id, 'status': status,
                'observation': ('verified' if observation_row and observation_row.ok
                                else 'failed' if observation_row else 'unknown'),
                'latest_test_id': observation_row.id if observation_row else None,
            })
        for agent in rows[MAX_BINDINGS:]:
            active, model_id = bool(agent.is_active), agent.model_id or None
            counts['total'] += 1
            counts['inactive' if not active else 'active_bound' if model_id else 'active_missing_model'] += 1
        current_latest = current_history[0] if current_history else None
        observation = ('not_required' if not connection.auth_required else
                       current_latest.credential_observation if current_latest else 'unknown')
        declared_state = ('disabled' if not connection.enabled else
                          'credential_missing' if connection.auth_required and not connection.secret_ref else 'declared')
        result.append({
            'connection_id': connection.id, 'label': connection.name, 'provider_kind': connection.kind,
            'enabled': bool(connection.enabled), 'auth_required': bool(connection.auth_required),
            'has_secret': bool(connection.secret_ref), 'public_config': public_config(connection),
            'config_fingerprint': fingerprint, 'declared_state': declared_state,
            'credential_observation': observation, 'binding_counts': counts,
            'agent_bindings': bindings, 'agent_bindings_truncated': len(rows) > MAX_BINDINGS,
            'latest_test': record_public(latest, test_fingerprint(
                connection, latest.model_id or '', latest.profile or 'full')) if latest else None,
        })
    return {'connections': result}
