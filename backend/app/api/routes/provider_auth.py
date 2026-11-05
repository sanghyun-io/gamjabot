"""Owner-scoped provider authentication lifecycle routes."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.provider_auth import (
    AuthMethodsResponse, AuthOperationTerminal, AuthPublicState, AuthStartRequest,
)
from app.services import auth_operations, connections, provider_catalog
from app.services.core_auth_broker import CoreAuthBrokerUnavailable


router = APIRouter()


@router.get("/{connection_id}/auth/methods", response_model=AuthMethodsResponse)
async def provider_auth_methods(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = connections.owned(db, connection_id, user.id)
    if row.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        manager()
        return AuthMethodsResponse(methods=({'method_id':'oauth_browser','available':True},))
    descriptor = provider_catalog.get_descriptor(row.provider_id or row.kind)
    coordinator = getattr(request.app.state, "connection_recovery", None)
    available = (frozenset() if coordinator is None
                 else frozenset(coordinator.available_auth_methods(row)))
    return AuthMethodsResponse(methods=tuple({
        "method_id": method.method_id,
        "available": method.method_id in available,
    } for method in descriptor.methods))


def auth_error(error: auth_operations.AuthOperationError) -> HTTPException:
    code = error.code if error.code in {
        "auth_method_unavailable", "configuration_changed", "idempotency_conflict",
        "auth_operation_busy", "owner_epoch_unavailable", "owner_epoch_changed",
        "credential_store_unavailable", "credential_recovery_required",
        "credential_cleanup_unconfirmed", "provider_authorization_denied",
        "provider_temporary_failure", "provider_invalid_response",
        "operation_cancelled", "operation_expired", "operation_interrupted",
    } else "internal_error"
    if code in {"configuration_changed", "idempotency_conflict",
                "auth_operation_busy", "owner_epoch_changed"}:
        status = 409
    elif code == "auth_method_unavailable":
        status = 422
    else:
        status = 503
    return HTTPException(status, detail={"code": code})


def auth_lifecycle_response(result) -> JSONResponse:
    try:
        if result.state in {"succeeded", "cancelled", "expired", "interrupted", "failed"}:
            payload = AuthOperationTerminal(
                operation_id=result.operation_id,
                state=result.state,
                result=result.result,
                error_code=result.error_code,
            ).model_dump(mode="json", exclude_none=True)
        else:
            payload = AuthPublicState(
                version=1,
                operation_id=result.operation_id,
                state=result.state,
                expires_at=result.expires_at,
                browser_url=result.browser_url,
                verification_uri=result.verification_uri,
                user_code=result.user_code,
                poll_interval=result.poll_interval,
                error_code=result.error_code,
            ).model_dump(mode="json", exclude_none=True)
            payload.setdefault("expires_at", None)
    except (AttributeError, TypeError, ValidationError):
        raise HTTPException(500, detail="Invalid provider auth operation state") from None
    return JSONResponse(payload, status_code=200)


@router.post("/{connection_id}/auth/{method}/operations")
async def start_provider_auth(
    connection_id: int,
    method: str,
    body: AuthStartRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    row = connections.owned(db, connection_id, user.id)
    if row.provider_id == 'chatgpt':
        if method != 'oauth_browser': raise HTTPException(422,detail={'code':'auth_method_unavailable'})
        from app.services.chatgpt_oauth import manager
        return manager().start(row,body.config_revision,body.auth_grant_revision,idempotency_key)
    coordinator = getattr(request.app.state, "connection_recovery", None)
    if coordinator is None:
        raise HTTPException(503, detail={"code": "owner_epoch_unavailable"})
    try:
        result = coordinator.start_auth(
            db, row, method, body.config_revision, body.auth_grant_revision,
            idempotency_key=idempotency_key,
        )
        return auth_lifecycle_response(result)
    except auth_operations.AuthOperationError as error:
        raise auth_error(error) from None
    except CoreAuthBrokerUnavailable:
        raise HTTPException(503, detail={"code": "owner_epoch_unavailable"}) from None
