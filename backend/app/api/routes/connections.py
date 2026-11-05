"""Owner-scoped connection metadata and explicit connectivity tests."""

import re
import asyncio
import hashlib
import struct
import zlib
import secrets
from time import monotonic
from copy import deepcopy

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError, BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.api.routes.provider_auth import auth_lifecycle_response
from app.core.database import get_db
from app.models.collaboration import ProviderConnection
from app.models.models import User
from app.schemas.connection import (ConnectionCreate, ConnectionRevisions,
                                    ConnectionTestPreview, ConnectionTestProfile,
                                    ConnectionUpdate, DisconnectRequest,
                                    VersionedConnectionCreate,
                                    VersionedConnectionUpdate)
from app.schemas.provider_catalog import ProviderCatalogResponse
from app.schemas.provider_models import ModelCatalog, ModelRefreshRequest, valid_model_id, MODEL_ID_PATTERN
from app.schemas.provider_auth import (
    DisconnectOperationPending, DisconnectOperationTerminal,
)
from app.schemas.readiness import TestHistory
from app.services import connections
from app.services import provider_catalog
from app.services import model_catalog
from app.services import readiness
from app.services.secrets import SecretStoreUnavailable
from app.services import auth_operations
from app.services.core_auth_broker import CoreAuthBrokerUnavailable
from app.services.connection_recovery import AuthLifecycleResult

router = APIRouter()
DIAGNOSTIC_REASONS = frozenset({
    'keychain_query_failed', 'keychain_interaction_required', 'credential_channel_unavailable',
    'credential_response_invalid', 'keychain_store_unavailable',
})


def problem(error: connections.ConnectionFailure | SecretStoreUnavailable,
            request: Request | None = None, owner_id: int | None = None):
    credential_error = (error if isinstance(error, SecretStoreUnavailable)
                        else getattr(error, 'credential_error', None))
    if credential_error is not None:
        detail = {'ok': False, 'code': 'secret_store_unavailable',
                  'message': connections.MESSAGES['secret_store_unavailable']}
        if request is not None and owner_id is not None:
            correlation_id = secrets.token_hex(16)
            reason = (credential_error.reason if credential_error.reason in DIAGNOSTIC_REASONS
                      else 'keychain_store_unavailable')
            native_detail = (
                credential_error.error_class in {'native_os_status', 'interaction_required'}
                and credential_error.operation is not None
                and credential_error.os_status is not None
            )
            error_class = (credential_error.error_class
                           if native_detail or credential_error.error_class in {
                               'bridge_transport', 'bridge_protocol', 'store_unavailable'}
                           else 'store_unavailable')
            record = {
                'correlation_id': correlation_id,
                'stage': 'provider_credential',
                'code': 'secret_store_unavailable',
                'reason': reason,
                'error_class': error_class,
            }
            if native_detail:
                record['operation'] = credential_error.operation
                record['os_status'] = credential_error.os_status
            records = getattr(request.app.state, 'connection_diagnostics', {})
            records[correlation_id] = (owner_id, record)
            while len(records) > 20:
                records.pop(next(iter(records)))
            request.app.state.connection_diagnostics = records
            detail['correlation_id'] = correlation_id
        return detail
    return {'ok': False, 'code': error.code, 'message': error.message}


def _failure_status(error) -> int:
    if (isinstance(error, SecretStoreUnavailable)
            or getattr(error, 'code', None) in {
                'storage_unavailable', 'credential_cleanup_unconfirmed',
                'owner_epoch_unavailable'}):
        return 503
    if getattr(error, 'code', None) in {
            'configuration_changed', 'credential_operation_pending'}:
        return 409
    return 422


def _recovery_error(error: auth_operations.AuthOperationError,
                    *, operation_lookup: bool = False) -> HTTPException:
    if operation_lookup:
        return HTTPException(404, detail={"code": "operation_not_found"})
    code = error.code if error.code in {
        "idempotency_conflict", "auth_operation_busy", "configuration_changed",
        "owner_epoch_unavailable", "owner_epoch_changed",
        "credential_store_unavailable", "credential_recovery_required",
        "credential_cleanup_unconfirmed",
    } else "internal_error"
    if code in {"idempotency_conflict", "auth_operation_busy", "configuration_changed",
                "owner_epoch_changed"}:
        status_code = 409
    elif code in {"owner_epoch_unavailable", "credential_store_unavailable",
                  "credential_recovery_required", "credential_cleanup_unconfirmed",
                  "internal_error"}:
        status_code = 503
    else:
        status_code = 422
    return HTTPException(status_code, detail={"code": code})


def _lifecycle_response(result, *, immediate_disconnect: bool = False) -> JSONResponse:
    """Project the service result through one closed public schema."""
    try:
        if result.state in {"pending", "finalizing"}:
            payload = DisconnectOperationPending(
                operation_id=result.operation_id, state=result.state,
            ).model_dump(mode="json")
            return JSONResponse(payload, status_code=202)
        terminal = DisconnectOperationTerminal(
            operation_id=result.operation_id,
            state=result.state,
            result=result.result,
            error_code=result.error_code,
        )
    except (AttributeError, TypeError, ValidationError):
        raise HTTPException(500, detail="Invalid provider operation state") from None
    payload = terminal.model_dump(mode="json", exclude_none=True)
    if immediate_disconnect and terminal.state == "succeeded":
        return JSONResponse(payload["result"], status_code=200)
    return JSONResponse(payload, status_code=200)


@router.get('/catalog', response_model=ProviderCatalogResponse)
async def provider_catalog_response(request: Request, user: User = Depends(get_current_user)):
    return ProviderCatalogResponse(version=1, providers=provider_catalog.get_catalog(
        personal_agent_runtime=connections.personal_agent_runtime(
            getattr(request.app.state, 'secret_store', None))))


@router.get('')
async def list_connections(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(ProviderConnection).filter_by(user_id=user.id).order_by(ProviderConnection.id).all()
    return [connections.public(row) for row in rows]


@router.get('/{connection_id}/models', response_model=ModelCatalog)
async def connection_models(connection_id: int, db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    connections.owned(db, connection_id, user.id)
    return model_catalog.cached_models(db, user.id, connection_id)


@router.get('/{connection_id}/revisions', response_model=ConnectionRevisions)
async def connection_revisions(connection_id: int, db: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    return {
        'version': 1,
        'config_revision': int(row.readiness_revision or 1),
        'auth_grant_revision': int(row.auth_grant_revision or 0),
    }


@router.post('/{connection_id}/models/refresh', response_model=ModelCatalog)
async def refresh_connection_models(connection_id: int, body: ModelRefreshRequest,
                                    request: Request, db: Session = Depends(get_db),
                                    user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    if (body.config_revision != int(row.readiness_revision or 1)
            or body.auth_grant_revision != int(row.auth_grant_revision or 0)):
        raise HTTPException(409, detail={
            'code': 'configuration_changed',
            'message': 'Connection changed before model refresh.',
        })

    async def discover(_row):
        config = await connections.resolve_discovery_connection_async(
            db, connection_id, user.id,
            getattr(request.app.state, 'secret_store', None) if row.auth_required else None,
            cache=getattr(request.app.state, 'credential_cache', None),
        )
        return await connections.discover_models(
            config, getattr(request.app.state, 'connection_transport', None))

    try:
        return await model_catalog.refresh_models(db, row, user.id, discover)
    except model_catalog.ModelRefreshFailure as error:
        raise HTTPException(error.status_code, detail={
            'code': error.code,
            'message': error.message,
        }) from None


@router.get('/diagnostics/{correlation_id}')
async def connection_diagnostic(correlation_id: str, request: Request,
                                user: User = Depends(get_current_user)):
    if not re.fullmatch(r'[0-9a-f]{32}', correlation_id):
        raise HTTPException(404, 'Connection diagnostic not found')
    owned_record = getattr(request.app.state, 'connection_diagnostics', {}).get(correlation_id)
    if owned_record is None or owned_record[0] != user.id:
        raise HTTPException(404, 'Connection diagnostic not found')
    return owned_record[1]


@router.post('', status_code=201)
async def create_connection(data: VersionedConnectionCreate | ConnectionCreate, request: Request,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        needs_store = bool(data.api_key) or isinstance(data, ConnectionCreate) and data.auth_required
        row = connections.create(
            db, user.id, data, connections.store_for(request.app) if needs_store else None)
        return connections.public(row)
    except (connections.ConnectionFailure, SecretStoreUnavailable) as error:
        raise HTTPException(_failure_status(error), detail=problem(error, request, user.id)) from None


@router.patch('/{connection_id}')
async def update_connection(connection_id: int,
                            data: VersionedConnectionUpdate | ConnectionUpdate,
                            request: Request, db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    old_ref = row.secret_ref
    try:
        changes = (data if isinstance(data, VersionedConnectionUpdate)
                   else data.model_dump(exclude_unset=True))
        saved = connections.update(
            db, row, changes,
            connections.store_for(request.app) if row.secret_ref or data.api_key else None)
        cache = getattr(request.app.state, 'credential_cache', None)
        if cache and old_ref:
            cache.clear(connections._cache_key(user.id, connection_id, old_ref))
        return connections.public(saved)
    except (connections.ConnectionFailure, SecretStoreUnavailable) as error:
        raise HTTPException(_failure_status(error), detail=problem(error, request, user.id)) from None


@router.post('/{connection_id}/disconnect')
async def disconnect_connection(connection_id: int, body: DisconnectRequest,
                                request: Request, db: Session = Depends(get_db),
                                user: User = Depends(get_current_user),
                                idempotency_key: str | None = Header(
                                    default=None, alias='Idempotency-Key')):
    row = connections.owned(db, connection_id, user.id)
    if row.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        return manager().disconnect(db,row,body.revoke_remote)
    coordinator = getattr(request.app.state, 'connection_recovery', None)
    if coordinator is not None:
        try:
            result = connections.disconnect_connection_durable(
                db, row, body.revoke_remote, recovery=coordinator,
                idempotency_key=idempotency_key,
                remote_revoker=getattr(request.app.state, 'connection_revoker', None),
            )
            return _lifecycle_response(result, immediate_disconnect=True)
        except auth_operations.AuthOperationError as error:
            raise _recovery_error(error) from None
        except CoreAuthBrokerUnavailable:
            raise HTTPException(503, detail={"code": "owner_epoch_unavailable"}) from None
    if (getattr(request.app.state, 'provider_recovery_mode', 'legacy')
            == 'core_managed_recovery_required'):
        raise HTTPException(
            503, detail={"code": "owner_epoch_unavailable"})
    old_ref = row.secret_ref
    try:
        result = connections.disconnect_connection(
            db, row, body.revoke_remote,
            connections.store_for(request.app) if old_ref else None,
            getattr(request.app.state, 'connection_revoker', None),
        )
        cache = getattr(request.app.state, 'credential_cache', None)
        if cache and old_ref and result.credential_state == 'missing':
            cache.clear(connections._cache_key(user.id, connection_id, old_ref))
        return result
    except (connections.ConnectionFailure, SecretStoreUnavailable) as error:
        cache = getattr(request.app.state, 'credential_cache', None)
        if (cache and old_ref and isinstance(error, connections.ConnectionFailure)
                and error.code == 'credential_cleanup_unconfirmed'):
            cache.clear(connections._cache_key(user.id, connection_id, old_ref))
        raise HTTPException(_failure_status(error), detail=problem(error, request, user.id)) from None


def _operation_id(value: str) -> str:
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise HTTPException(404, detail={"code": "operation_not_found"})
    return value


def _recovery_coordinator(request: Request):
    coordinator = getattr(request.app.state, 'connection_recovery', None)
    if coordinator is None:
        raise HTTPException(503, detail={"code": "owner_epoch_unavailable"})
    return coordinator


@router.get('/{connection_id}/auth/operations/{operation_id}')
async def connection_operation_status(connection_id: int, operation_id: str,
                                      request: Request, db: Session = Depends(get_db),
                                      user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    operation_id = _operation_id(operation_id)
    if row.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        return manager().status(user.id,row.id,operation_id,cancel=False)
    try:
        result = _recovery_coordinator(request).operation_status(
            db, user.id, row.id, operation_id)
        return (auth_lifecycle_response(result)
                if isinstance(result, AuthLifecycleResult)
                else _lifecycle_response(result))
    except auth_operations.AuthOperationError as error:
        raise _recovery_error(error, operation_lookup=True) from None
    except CoreAuthBrokerUnavailable:
        raise HTTPException(503, detail={"code": "owner_epoch_unavailable"}) from None


@router.delete('/{connection_id}/auth/operations/{operation_id}')
async def cancel_connection_operation(connection_id: int, operation_id: str,
                                      request: Request, db: Session = Depends(get_db),
                                      user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    operation_id = _operation_id(operation_id)
    if row.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        return manager().status(user.id,row.id,operation_id,cancel=True)
    try:
        result = _recovery_coordinator(request).cancel_operation(
            db, user.id, row.id, operation_id)
        return (auth_lifecycle_response(result)
                if isinstance(result, AuthLifecycleResult)
                else _lifecycle_response(result))
    except auth_operations.AuthOperationError as error:
        raise _recovery_error(error, operation_lookup=True) from None
    except CoreAuthBrokerUnavailable:
        raise HTTPException(503, detail={"code": "owner_epoch_unavailable"}) from None


@router.delete('/{connection_id}', status_code=204)
async def delete_connection(connection_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    if row.provider_id == 'chatgpt' and row.secret_ref:
        raise HTTPException(409,detail={'code':'disconnect_first','message':'Disconnect ChatGPT locally before deleting this connection.'})
    old_ref = row.secret_ref
    try:
        connections.delete(db, row, connections.store_for(request.app) if row.secret_ref else None)
        cache = getattr(request.app.state, 'credential_cache', None)
        if cache and old_ref:
            cache.clear(connections._cache_key(user.id, connection_id, old_ref))
    except SecretStoreUnavailable as error:
        from fastapi import HTTPException
        raise HTTPException(503, detail=problem(error, request, user.id)) from None


def _tested_model(row: ProviderConnection, model_id: str | None) -> str:
    selected = row.default_model_id if model_id is None else model_id
    if selected is None:
        raise HTTPException(422, detail={'code': 'missing_model', 'message': connections.MESSAGES['missing_model']})
    if not valid_model_id(selected):
        raise HTTPException(422, detail={'code': 'invalid_request', 'message': 'Invalid model ID.'})
    return selected


@router.get('/{connection_id}/test-preview', response_model=ConnectionTestPreview)
async def test_connection_preview(connection_id: int, model_id: str | None = None,
                                  profile: ConnectionTestProfile = 'full',
                                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    tested_model = _tested_model(row, model_id)
    return {'profile': profile, 'model_id': tested_model,
            'max_request_count': connections.test_request_limit(row.kind, row.capabilities, profile)}


@router.post('/{connection_id}/test')
async def test_connection(connection_id: int, request: Request, model_id: str | None = None,
                          profile: ConnectionTestProfile = 'full',
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owner_id = user.id
    row = connections.owned(db, connection_id, owner_id)
    tested_model = _tested_model(row, model_id)
    original = {field: deepcopy(getattr(row, field)) for field in
                ('kind', 'base_url', 'default_model_id', 'secret_ref', 'enabled', 'auth_required',
                 'capabilities', 'readiness_revision', 'provider_id', 'protocol_id',
                 'auth_method_id', 'public_config_version', 'public_config',
                 'auth_grant_revision', 'credential_state')}
    fingerprint = readiness.test_fingerprint(row, tested_model, profile)
    max_request_count = connections.test_request_limit(row.kind, row.capabilities, profile)
    physical_budget = connections.PhysicalRequestBudget(max_request_count)
    meter = connections.RequestMeter(
        getattr(request.app.state, 'connection_transport', None),
        physical_request_budget=physical_budget)
    actual_request_count = physical_budget.actual_count
    started = monotonic()
    result = None
    verified = None
    try:
        owner_epochs = getattr(request.app.state, 'owner_epoch_provider', None)
        owner_epoch = owner_epochs.current(owner_id) if owner_epochs is not None else None
        if row.provider_id == 'chatgpt' and not owner_epoch:
            raise connections.ConnectionFailure('owner_epoch_unavailable', connections.MESSAGES['owner_epoch_unavailable'])
        config = await connections.resolve_connection_async(
            db, connection_id, owner_id,
            getattr(request.app.state, 'secret_store', None) if row.auth_required else None,
            release_snapshot_transaction=True, cache=getattr(request.app.state, 'credential_cache', None),
            model_id_override=model_id)
        core_managed = connections.is_core_managed_credential(config)
        if core_managed:
            if owner_epochs is None or owner_epochs.current(owner_id) != owner_epoch:
                raise connections.ConnectionFailure('configuration_changed', connections.MESSAGES['configuration_changed'])
            config = connections.bind_native_runtime(config, owner_epoch)
            status = await connections.probe_native(
                config, getattr(request.app.state, 'provider_client', None),
                physical_request_budget=physical_budget)
        else:
            status = await connections.probe(config, meter)
        capabilities = deepcopy(original['capabilities'] or {})
        values = {'readiness_revision': original['readiness_revision']}
        options = None
        if profile == 'full':
            options = await connections.probe_capabilities(
                config, meter,
                core_transport=(getattr(request.app.state, 'provider_client', None)
                                if core_managed else None),
                physical_request_budget=physical_budget,
            )
            existing_verified = dict(capabilities.get('verified', {}))
            models = dict(existing_verified.get('models', {}))
            models[config.model_id] = options
            values = {'capabilities': {**capabilities, 'declared': capabilities.get('declared', {}),
                                       'verified': {'models': models}}}
            features=values['capabilities'].setdefault('model_features', {})
            feature=features.get(config.model_id)
            if config.provider_id == 'chatgpt':
                from app.services.model_features import record as feature_record
                feature=deepcopy(feature_record(config))
                feature.update(model_id=config.model_id,base_url=config.base_url,config_revision=config.config_revision,source='saved_connection_tests')
                feature.setdefault('tested',{})['text_generation']=True
                features[config.model_id]=feature
            if feature:
                for option,target in [('tools','tool_calling'),('vision','image_input')]:
                    if capabilities.get('declared',{}).get(option) is True:feature.setdefault('tested',{})[target]=options.get(option) is True
        if row.provider_id == 'chatgpt' and (owner_epochs is None or owner_epochs.current(owner_id) != owner_epoch):
            raise connections.ConnectionFailure('configuration_changed', connections.MESSAGES['configuration_changed'])
        saved = db.execute(update(ProviderConnection).where(
            ProviderConnection.id == connection_id, ProviderConnection.user_id == owner_id,
            *(getattr(ProviderConnection, field) == value for field, value in original.items())
        ).values(**values)
            .execution_options(synchronize_session=False)).rowcount
        if saved != 1:
            db.rollback()
            raise connections.ConnectionFailure('configuration_changed', connections.MESSAGES['configuration_changed'])
        db.commit()
        if options is not None:
            status['verified'] = options
        result, verified = status, options
    except (connections.ConnectionFailure, SecretStoreUnavailable) as error:
        db.rollback()
        result = problem(error, request, owner_id)
    finally:
        await meter.close_owned_transport()
    actual_request_count = physical_budget.actual_count
    result.update(profile=profile, model_id=tested_model, max_request_count=max_request_count,
                  actual_request_count=actual_request_count)
    record = readiness.record_test(
        db, owner_id=owner_id, connection_id=connection_id,
        config_revision=int(original['readiness_revision'] or 1), fingerprint=fingerprint,
        model_id=tested_model, ok=bool(result['ok']), code=result['code'],
        auth_required=bool(original['auth_required']), verified=verified,
        duration_ms=round((monotonic() - started) * 1000),
        profile=profile, actual_request_count=actual_request_count,
    )
    if record is not None:
        result['record'] = record
    return result


@router.get('/{connection_id}/test-history', response_model=TestHistory)
async def connection_test_history(connection_id: int, limit: int = 20,
                                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not 1 <= limit <= 20:
        raise HTTPException(422, detail={'code': 'invalid_request', 'message': 'History limit must be from 1 to 20.'})
    row = connections.owned(db, connection_id, user.id)
    return readiness.history(db, row, limit)


@router.post('/{connection_id}/unlock', status_code=202)
async def unlock_connection(connection_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    if not row.auth_required or not row.secret_ref:
        raise HTTPException(422, detail={'code': 'missing_secret', 'message': connections.MESSAGES['missing_secret']})
    cache = getattr(request.app.state, 'credential_cache', None)
    if cache is None:
        raise HTTPException(503, detail={'code': 'secret_store_unavailable', 'message': connections.MESSAGES['secret_store_unavailable']})
    store = connections.store_for(request.app)
    if not getattr(store, 'supports_interactive_unlock', True):
        return {'state': 'reentry_required', 'action': 'reenter_api_key'}
    state = cache.start(connections._cache_key(user.id, connection_id, row.secret_ref), store, row.secret_ref)
    return {'state': state}


@router.get('/{connection_id}/unlock')
async def unlock_status(connection_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    if not row.auth_required or not row.secret_ref:
        return {'state': 'locked'}
    cache = getattr(request.app.state, 'credential_cache', None)
    return {'state': cache.status(connections._cache_key(user.id, connection_id, row.secret_ref)) if cache else 'locked'}

# Local setup uses a public loopback address and never reads an API key or creates a connection during discovery.
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from app.services import model_features

class LocalModelsRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider_id:Literal['ollama','lm-studio','local-openai']
    base_url:str=Field(min_length=1,max_length=2048)

class FeatureCheckRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model_id:str=Field(min_length=1,max_length=200,pattern=r'^[A-Za-z0-9_][A-Za-z0-9._:-]*(?:/[A-Za-z0-9_][A-Za-z0-9._:-]*)*$')

@router.post('/local/models',response_model=ModelCatalog)
async def local_models(body:LocalModelsRequest,user:User=Depends(get_current_user)):
    from datetime import datetime,timezone
    fields={'base_url':body.base_url.strip().rstrip('/')}
    origin=provider_catalog.resolve_origin(body.provider_id,fields)
    config=connections.ConnectionConfig(0,'compatible','',origin,None,{},provider_id=body.provider_id,protocol_id='openai_chat_completions',auth_method_id='local_none',public_config_version=1,public_fields=fields,auth_grant_revision=0,credential_state='not_required',owner_id=user.id,config_revision=1)
    try:
        items,truncated,observed=await connections.discover_models(config)
        return ModelCatalog(models=items,truncated=truncated,observed_at=observed,stale=False)
    except Exception:raise HTTPException(422,{'code':'local_model_list_unavailable','message':'Start the local model server, load/download a model and confirm its /v1 address.'}) from None

@router.get('/{connection_id}/features')
async def get_model_features(connection_id:int,request:Request,model_id:str|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    row=connections.owned(db,connection_id,user.id)
    if model_id is not None and not valid_model_id(model_id):raise HTTPException(422,{'code':'missing_model'})
    config=connections.ConnectionConfig(row.id,row.kind,model_id or row.default_model_id,row.base_url,None,deepcopy(row.capabilities or {}),provider_id=row.provider_id,protocol_id=row.protocol_id,auth_method_id=row.auth_method_id or ('api_key' if row.auth_required else 'local_none'),config_revision=row.readiness_revision,auth_grant_revision=int(row.auth_grant_revision or 0))
    model_features.bind_saved_text_test(db,row,config)
    return JSONResponse(model_features.report(config),headers={'Cache-Control':'no-store'})

@router.post('/{connection_id}/features/check')
async def check_model_features(connection_id:int,body:FeatureCheckRequest,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    import asyncio
    row=connections.owned(db,connection_id,user.id);revision=int(row.readiness_revision or 1);grant=int(row.auth_grant_revision or 0)
    config=await connections.resolve_connection_async(db,connection_id,user.id,getattr(request.app.state,'secret_store',None),cache=getattr(request.app.state,'credential_cache',None),model_id_override=body.model_id)
    if not model_features.local(config):raise HTTPException(422,{'code':'local_model_required'})
    epochs=getattr(request.app.state,'owner_epoch_provider',None);epoch=epochs.current(user.id) if epochs else None
    locks=getattr(request.app.state,'local_feature_locks',None)
    if locks is None:locks={};request.app.state.local_feature_locks=locks
    lock=locks.setdefault(user.id,asyncio.Lock())
    if lock.locked():raise HTTPException(409,{'code':'local_feature_check_busy'})
    try:
        async with lock:
            async with asyncio.timeout(180):evidence=await model_features.probe(config)
    except Exception:raise HTTPException(422,{'code':'local_model_check_failed','message':'Local feature check could not complete. No support was inferred.'}) from None
    db.rollback();current=connections.owned(db,connection_id,user.id)
    if epochs and epochs.current(user.id)!=epoch:raise HTTPException(409,{'code':'configuration_changed'})
    if int(current.readiness_revision or 1)!=revision or int(current.auth_grant_revision or 0)!=grant:raise HTTPException(409,{'code':'configuration_changed'})
    caps=deepcopy(current.capabilities or {});items=caps.setdefault('model_features',{});items[body.model_id]=evidence
    # Keep at most32 exact-model results per connection; changing configuration naturally invalidates them.
    if len(items)>32:del items[next(iter(items))]
    updated=db.execute(update(ProviderConnection).where(ProviderConnection.id==connection_id,ProviderConnection.user_id==user.id,ProviderConnection.readiness_revision==revision).values(capabilities=caps).execution_options(synchronize_session=False)).rowcount
    if updated!=1:db.rollback();raise HTTPException(409,{'code':'configuration_changed'})
    db.commit();config.capabilities=caps
    return JSONResponse(model_features.report(config),headers={'Cache-Control':'no-store'})

# An explicit, idempotent image-only feature check. The command ledger is small
# metadata in the connection record; no prompt, image or credential is stored.
class ImageInputCheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    config_revision: int = Field(ge=1)
    auth_grant_revision: int = Field(ge=0)


_image_checks_running: set[tuple[int, int, str]] = set()
_IMAGE_CHECK_ERROR_CODES = frozenset({
    'unreachable', 'provider_timeout', 'invalid_credential', 'rate_limited',
    'provider_error', 'provider_invalid_response', 'provider_unavailable',
    'invalid_configuration', 'unsupported_option', 'model_not_found',
    'missing_secret', 'secret_store_unavailable', 'credential_locked',
    'configuration_changed', 'owner_epoch_unavailable', 'image_check_unavailable',
    'quota_exhausted', 'unsupported_model_protocol',
})
_IMAGE_CHECK_PHASES = frozenset({'credential', 'prepare', 'first_image', 'second_image'})


_IMAGE_COLOR_CATEGORIES = frozenset({'red', 'blue', 'other', 'empty'})


def _image_solid_color(rgb: bytes) -> bytes:
    """Trusted 64×64 synthetic RGB PNG; never reads user image data."""
    if rgb not in (b'\xff\x00\x00', b'\x00\x00\xff'):
        raise ValueError('unsupported synthetic color')
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))
    rows = (b'\x00' + rgb * 64) * 64
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 64, 64, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def _image_color_category(value):
    if not isinstance(value, str):
        return 'other'
    normalized = value.strip().lower()
    return normalized if normalized in ('red', 'blue') else 'empty' if not normalized else 'other'


def _image_command_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key):
        raise HTTPException(422, {'code': 'image_check_invalid_key'})
    return hashlib.sha256(key.encode('ascii')).hexdigest()


def _image_epoch(request, owner_id):
    epochs = getattr(request.app.state, 'owner_epoch_provider', None)
    epoch = epochs.current(owner_id) if epochs else None
    if not isinstance(epoch, str) or not epoch:
        raise HTTPException(503, {'code': 'owner_epoch_unavailable'})
    return hashlib.sha256(epoch.encode()).hexdigest(), epoch


def _image_command_view(item, running):
    state = item['status']
    if state == 'pending' and not running:
        state = 'unknown'
    result = {'version': 1, 'model_id': item['model_id'], 'status': state,
            'config_revision': item['config_revision'], 'auth_grant_revision': item['auth_grant_revision'],
            'max_generation_requests': 2, 'max_catalog_requests': item['max_catalog_requests'],
            'max_metered_model_http_requests': 2 + item['max_catalog_requests'],
            'actual_generation_requests': item.get('actual_generation_requests'),
            'actual_catalog_requests': item.get('actual_catalog_requests'),
            'actual_metered_model_http_requests': item.get('actual_metered_model_http_requests')}
    if (state == 'unknown' and isinstance(item.get('error_code'), str) and isinstance(item.get('phase'), str)
            and item['error_code'] in _IMAGE_CHECK_ERROR_CODES and item['phase'] in _IMAGE_CHECK_PHASES):
        result.update(error_code=item['error_code'], phase=item['phase'])
    colors = item.get('color_results')
    if (state == 'failed' and isinstance(colors, list) and len(colors) == 2
            and all(isinstance(color, str) and color in _IMAGE_COLOR_CATEGORIES for color in colors)):
        result['color_results'] = colors
    return result


@router.get('/{connection_id}/features/image-input/preview')
def image_input_preview(connection_id: int, model_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = connections.owned(db, connection_id, user.id)
    model = _tested_model(row, model_id)
    catalog = 2 if row.provider_id == 'chatgpt' else 0
    return JSONResponse({'version': 1, 'model_id': model, 'max_generation_requests': 2,
                         'max_catalog_requests': catalog, 'max_metered_model_http_requests': 2 + catalog,
                         'config_revision': int(row.readiness_revision or 1),
                         'auth_grant_revision': int(row.auth_grant_revision or 0)}, headers={'Cache-Control': 'no-store'})


@router.get('/{connection_id}/features/image-input/commands/{key}')
def image_input_command(connection_id: int, key: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    digest = _image_command_key(key)
    row = connections.owned(db, connection_id, user.id)
    item = (row.capabilities or {}).get('image_check_commands', {}).get(digest)
    if not isinstance(item, dict):
        raise HTTPException(404, {'code': 'image_check_not_found'})
    return JSONResponse(_image_command_view(item, (user.id, connection_id, digest) in _image_checks_running), headers={'Cache-Control': 'no-store'})


@router.post('/{connection_id}/features/image-input/check')
async def image_input_check(connection_id: int, body: ImageInputCheckRequest, request: Request,
                            idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    from app.services import model_features
    from app.services.providers import adapter_for, ProviderFailure, ProviderRequest, VisionInput
    from dataclasses import replace
    digest = _image_command_key(idempotency_key)
    epoch_hash, epoch = _image_epoch(request, user.id)
    row = connections.owned(db, connection_id, user.id)
    revision, grant = int(row.readiness_revision or 1), int(row.auth_grant_revision or 0)
    pinned_fields = ('kind','base_url','default_model_id','secret_ref','enabled','auth_required',
                     'provider_id','protocol_id','auth_method_id','public_config_version','public_config',
                     'credential_state','readiness_revision','auth_grant_revision')
    pinned = {name: deepcopy(getattr(row, name)) for name in pinned_fields}
    original = deepcopy(row.capabilities or {})
    commands = original.get('image_check_commands', {})
    if not isinstance(commands, dict):
        raise HTTPException(409, {'code': 'image_check_unavailable'})
    existing = commands.get(digest)
    identity = (user.id, connection_id, digest)
    if existing:
        if (existing.get('model_id'), existing.get('config_revision'), existing.get('auth_grant_revision'), existing.get('owner_epoch_sha256')) != (body.model_id, body.config_revision, body.auth_grant_revision, epoch_hash):
            raise HTTPException(409, {'code': 'image_check_key_conflict'})
        return JSONResponse(_image_command_view(existing, identity in _image_checks_running), headers={'Cache-Control': 'no-store'})
    if (revision, grant) != (body.config_revision, body.auth_grant_revision) or not row.enabled:
        raise HTTPException(409, {'code': 'configuration_changed'})
    if len(commands) >= 64:
        raise HTTPException(409, {'code': 'image_check_capacity'})
    claimed = deepcopy(original)
    item = {'model_id': body.model_id, 'config_revision': revision, 'auth_grant_revision': grant,
            'owner_epoch_sha256': epoch_hash, 'status': 'pending', 'actual_generation_requests': None,
            'actual_catalog_requests': None, 'actual_metered_model_http_requests': None,
            'max_catalog_requests': 2 if row.provider_id == 'chatgpt' else 0}
    claimed.setdefault('image_check_commands', {})[digest] = item
    changed = db.execute(update(ProviderConnection).where(ProviderConnection.id == connection_id,
        ProviderConnection.user_id == user.id, *(getattr(ProviderConnection, name) == value for name, value in pinned.items()),
        ProviderConnection.capabilities == row.capabilities)
        .values(capabilities=claimed).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback(); raise HTTPException(409, {'code': 'configuration_changed'})
    db.commit()
    _image_checks_running.add(identity)
    budget = connections.PhysicalRequestBudget(2 + item['max_catalog_requests'])
    class ImageCheckMeter(connections.RequestMeter):
        # Meter model endpoint GET catalog calls separately from generation.
        # OAuth token-manager refresh traffic is outside this model HTTP budget.
        def __init__(self, transport, physical_request_budget):
            super().__init__(transport, physical_request_budget=physical_request_budget)
            self.catalog_count = 0
        async def handle_async_request(self, http_request):
            if http_request.method == 'GET':
                if self.catalog_count >= item['max_catalog_requests']:
                    raise connections.ConnectionFailure('provider_error', connections.MESSAGES['provider_error'])
                self.catalog_count += 1
            return await super().handle_async_request(http_request)
    meter = ImageCheckMeter(getattr(request.app.state, 'connection_transport', None), budget)
    outcome = 'unknown'
    failure_phase = 'credential'
    failure_code = None
    color_results = None
    try:
        config = await connections.resolve_connection_async(db, connection_id, user.id,
            getattr(request.app.state, 'secret_store', None), release_snapshot_transaction=True,
            cache=getattr(request.app.state, 'credential_cache', None), model_id_override=body.model_id)
        failure_phase = 'prepare'
        if _image_epoch(request, user.id)[0] != epoch_hash:
            raise ValueError('owner changed')
        db.expire_all(); current = connections.owned(db, connection_id, user.id)
        if ({name: getattr(current, name) for name in pinned_fields} != pinned or current.capabilities != claimed):
            raise ValueError('configuration changed')
        if model_features.local(config):
            raise ValueError('local check uses existing route')
        core_managed = connections.is_core_managed_credential(config)
        if core_managed:
            config = connections.bind_native_runtime(config, epoch)
        temporary_caps = deepcopy(config.capabilities or {})
        temporary_caps.setdefault('declared', {})['vision'] = True
        temporary_caps.setdefault('verified', {}).setdefault('models', {})[body.model_id] = {
            'model_id': body.model_id, 'base_url': config.base_url, 'vision': True}
        # This provisional evidence exists only inside the two synthetic requests.
        temporary_caps.setdefault('model_features', {})[body.model_id] = {
            'model_id': body.model_id, 'base_url': config.base_url, 'config_revision': revision,
            'tested': {'image_input': True}, 'image_check': {'verified': True, 'model_id': body.model_id,
                'base_url': config.base_url, 'config_revision': revision, 'auth_grant_revision': grant}}
        temporary = replace(config, capabilities=temporary_caps)
        adapter = adapter_for(config.protocol_id, config.provider_id, meter,
            core_transport=getattr(request.app.state, 'provider_client', None) if core_managed else None,
            physical_request_budget=budget)
        probe = ProviderRequest(connection=temporary, model_id=body.model_id, system='Answer with exactly one word: red or blue. No other words or punctuation.',
            agent_id=(config.owner_id or 0) if core_managed else 0,
            room_id=config.connection_id if core_managed else 0,
            events=[{'seq': 1, 'event_id': 'connection-test', 'actor_type': 'user',
                     'actor_id': config.owner_id or 0, 'actor_name_snapshot': 'Connection test',
                     'kind': 'message', 'payload': {'content': 'Name the color of the image pixel.'}}] if core_managed else [],
            watermark_seq=1 if core_managed else 0,
            trigger_event_id='connection-test' if core_managed else None,
            vision=VisionInput(_image_solid_color(b'\xff\x00\x00'), 'image/png', 'Is the solid image red or blue? Reply with exactly one word: red or blue.'))
        async with asyncio.timeout(120):
            failure_phase = 'first_image'
            first = await adapter.generate(probe)
            failure_phase = 'prepare'
            if _image_epoch(request, user.id)[0] != epoch_hash:
                raise ValueError('owner changed')
            db.expire_all(); current = connections.owned(db, connection_id, user.id)
            if ({name: getattr(current, name) for name in pinned_fields} != pinned or current.capabilities != claimed):
                raise ValueError('configuration changed')
            failure_phase = 'second_image'
            second = await adapter.generate(replace(probe, vision=VisionInput(_image_solid_color(b'\x00\x00\xff'), 'image/png', 'Is the solid image red or blue? Reply with exactly one word: red or blue.')))
        color_results = [_image_color_category(first.text), _image_color_category(second.text)]
        outcome = 'verified' if color_results == ['red', 'blue'] else 'failed'
    except (ProviderFailure, connections.ConnectionFailure) as error:
        failure_code = error.code if isinstance(error.code, str) and error.code in _IMAGE_CHECK_ERROR_CODES else 'image_check_unavailable'
    except TimeoutError:
        failure_code = 'provider_timeout'
    except Exception:
        failure_code = 'image_check_unavailable'
    finally:
        await meter.close_owned_transport()
        _image_checks_running.discard(identity)
    try:
        if _image_epoch(request, user.id)[0] != epoch_hash:
            raise ValueError('owner changed')
        db.expire_all(); current = connections.owned(db, connection_id, user.id)
        if ({name: getattr(current, name) for name in pinned_fields} != pinned or current.capabilities != claimed):
            raise ValueError('configuration changed')
        final = deepcopy(claimed)
        final_item = final['image_check_commands'][digest]
        actual = budget.actual_count
        final_item.update(status=outcome,
            actual_generation_requests=None if actual is None else actual - meter.catalog_count,
            actual_catalog_requests=None if actual is None else meter.catalog_count,
            actual_metered_model_http_requests=actual)
        if outcome == 'unknown' and failure_code:
            final_item.update(error_code=failure_code, phase=failure_phase)
        if outcome == 'failed' and color_results is not None:
            final_item['color_results'] = color_results
        if outcome in ('verified', 'failed'):
            model = final.setdefault('model_features', {}).get(body.model_id, {})
            if not (model.get('model_id') == body.model_id and model.get('base_url') == config.base_url and model.get('config_revision') == revision):
                model = {'model_id': body.model_id, 'base_url': config.base_url, 'config_revision': revision}
            model.setdefault('tested', {})['image_input'] = outcome == 'verified'
            model['image_check'] = {'verified': outcome == 'verified', 'model_id': body.model_id, 'base_url': config.base_url,
                                    'config_revision': revision, 'auth_grant_revision': grant}
            final['model_features'][body.model_id] = model
            verified_root = final.setdefault('verified', {})
            # Older connections stored exact-model evidence as a flat object.
            # Preserve only evidence explicitly bound to this model and origin.
            legacy = (deepcopy(verified_root) if 'models' not in verified_root
                      and verified_root.get('model_id') == body.model_id
                      and verified_root.get('base_url') == config.base_url else {})
            verified = verified_root.setdefault('models', {}).get(body.model_id, legacy)
            if verified.get('base_url') not in (None, config.base_url):
                verified = {}
            verified.update(model_id=body.model_id, base_url=config.base_url, vision=outcome == 'verified')
            final['verified']['models'][body.model_id] = verified
        changed = db.execute(update(ProviderConnection).where(ProviderConnection.id == connection_id,
            ProviderConnection.user_id == user.id, *(getattr(ProviderConnection, name) == value for name, value in pinned.items()),
            ProviderConnection.capabilities == current.capabilities)
            .values(capabilities=final).execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            db.rollback(); raise ValueError('configuration changed')
        db.commit()
        return JSONResponse(_image_command_view(final_item, False), headers={'Cache-Control': 'no-store'})
    except Exception:
        db.rollback()
        return JSONResponse(_image_command_view(item, False), headers={'Cache-Control': 'no-store'})
