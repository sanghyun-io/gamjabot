"""Owner-scoped cached model reads and explicit revision-bound refreshes."""

from datetime import UTC, datetime, timedelta
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.collaboration import ConnectionModelCache, ProviderConnection
from app.schemas.provider_models import ModelCatalog, ModelItem
from app.services import provider_catalog, readiness


TTL = timedelta(hours=1)


class ModelRefreshFailure(Exception):
    def __init__(self, code: str, message: str = "Model refresh failed.", status_code: int = 502):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(code)


def _grant(row: ProviderConnection) -> int:
    return int(row.auth_grant_revision or 0)


def _fingerprint(row: ProviderConnection) -> str:
    return readiness.config_fingerprint(row)


def _static(row: ProviderConnection) -> tuple[ModelItem, ...]:
    normalized = provider_catalog.normalize_legacy_connection(row) if row.provider_id is None else None
    provider_id = row.provider_id or normalized.provider_id
    descriptor = provider_catalog.get_descriptor(provider_id)
    return tuple(ModelItem(id=item, label=item, source="configured")
                 for item in sorted(set(descriptor.model_source.static_model_ids)))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def cached_models(db: Session, owner_id: int, connection_id: int) -> ModelCatalog:
    connection = db.query(ProviderConnection).filter_by(
        id=connection_id, user_id=owner_id).first()
    if connection is None:
        from fastapi import HTTPException
        raise HTTPException(404, "Connection not found")
    descriptor = provider_catalog.get_descriptor(
        connection.provider_id or provider_catalog.normalize_legacy_connection(connection).provider_id)
    if descriptor.model_source.strategy == "configured":
        return ModelCatalog(observed_at=None, stale=False, truncated=False, models=_static(connection))
    cache = db.query(ConnectionModelCache).filter_by(
        owner_id=owner_id, connection_id=connection_id).first()
    if cache is None:
        return ModelCatalog(observed_at=None, stale=True, truncated=False, models=())
    observed_at = _aware(cache.observed_at)
    stale = (
        cache.config_revision != int(connection.readiness_revision or 1)
        or cache.auth_grant_revision != _grant(connection)
        or cache.config_fingerprint != _fingerprint(connection)
        or observed_at is None
        or datetime.now(UTC) - observed_at >= TTL
    )
    try:
        items = tuple(ModelItem.model_validate(item) for item in (cache.models or [])[:200])
    except Exception:
        items, stale = (), True
    return ModelCatalog(
        observed_at=observed_at, stale=stale, truncated=bool(cache.truncated), models=items)


async def refresh_models(db: Session, connection: ProviderConnection, actor: int, discovery) -> ModelCatalog:
    if connection.user_id != actor:
        raise ModelRefreshFailure("not_found", "Connection not found", 404)
    revision = int(connection.readiness_revision or 1)
    grant = _grant(connection)
    fingerprint = _fingerprint(connection)
    connection_id = connection.id
    descriptor = provider_catalog.get_descriptor(
        connection.provider_id or provider_catalog.normalize_legacy_connection(connection).provider_id)
    failure = None
    try:
        if descriptor.model_source.strategy == "configured":
            raw_items = [item.model_dump() for item in _static(connection)]
            truncated, observed_at = False, None
        else:
            raw_items, truncated, observed_at = await discovery(connection)
        items = tuple(ModelItem.model_validate(item) for item in raw_items)
    except ModelRefreshFailure as error:
        failure = error
    except Exception as error:
        code = getattr(error, "code", "provider_error")
        statuses = {
            "invalid_credential": 401, "rate_limited": 429,
            "unreachable": 503, "redirect_refused": 502,
            "invalid_response": 502, "provider_error": 502,
            "missing_secret": 422, "credential_locked": 423,
            "secret_store_unavailable": 503, "connection_disabled": 422,
        }
        failure = ModelRefreshFailure(code, "Model refresh failed.", statuses.get(code, 502))
    db.rollback()
    current = db.query(ProviderConnection).filter_by(
        id=connection_id, user_id=actor).first()
    if (current is None or int(current.readiness_revision or 1) != revision
            or _grant(current) != grant or _fingerprint(current) != fingerprint):
        raise ModelRefreshFailure("configuration_changed", "Connection changed during model refresh.", 409)
    if failure is not None:
        raise failure
    if descriptor.model_source.strategy == "configured":
        return ModelCatalog(observed_at=None, stale=False, truncated=False, models=items)
    saved = db.execute(text("""INSERT INTO connection_model_cache
        (owner_id,connection_id,config_revision,auth_grant_revision,config_fingerprint,
         observed_at,truncated,models,updated_at)
        SELECT :owner,:connection,:revision,:grant,:fingerprint,:observed,:truncated,
               :models,:updated
        FROM provider_connections
        WHERE id=:connection AND user_id=:owner
          AND readiness_revision=:revision
          AND COALESCE(auth_grant_revision,0)=:grant
        ON CONFLICT(owner_id,connection_id) DO UPDATE SET
          config_revision=excluded.config_revision,
          auth_grant_revision=excluded.auth_grant_revision,
          config_fingerprint=excluded.config_fingerprint,
          observed_at=excluded.observed_at,
          truncated=excluded.truncated,
          models=excluded.models,
          updated_at=excluded.updated_at"""), {
              "owner": actor, "connection": connection_id, "revision": revision,
              "grant": grant, "fingerprint": fingerprint, "observed": observed_at,
              "truncated": bool(truncated),
              "models": json.dumps([item.model_dump() for item in items], separators=(",", ":")),
              "updated": datetime.now(UTC),
          }).rowcount
    if saved != 1:
        db.rollback()
        raise ModelRefreshFailure(
            "configuration_changed", "Connection changed during model refresh.", 409)
    db.commit()
    return ModelCatalog(
        observed_at=observed_at, stale=False, truncated=bool(truncated), models=items)
