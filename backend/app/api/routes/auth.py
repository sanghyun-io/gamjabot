"""
Authentication API routes
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import jwt
from jwt import PyJWTError as JWTError
import bcrypt
import secrets
import re

from app.core.database import get_db
from app.core.config import settings
from app.models.models import User
from app.schemas.auth import UserCreate, UserLogin, UserResponse, Token, RestoreHint
from app.services.core_auth_broker import CoreAuthBrokerUnavailable
from app.services import auth_operations
from app.services import vault_accounts
from app.services.remembered_login import (REMEMBER_ERROR_CLASSES, REMEMBER_REASONS,
                                           current_reference, remember, restore,
                                           restore_credential, revoke, secret_store)
from app.services.secrets import SecretInteractionRequired, SecretReentryRequired, SecretStoreUnavailable
from app.services.auth_diagnostics import RESTORE_REASONS, valid_auth_diagnostic, make_auth_report_diagnostic
from app.services.diagnostics import emit_detailed_diagnostic

router = APIRouter()
security = HTTPBearer()


def _record_auth_diagnostic(request: Request, record: dict) -> None:
    if not valid_auth_diagnostic(record, record.get("correlation_id"), record.get("stage")):
        return
    name = record["stage"] + "_diagnostics"
    records = getattr(request.app.state, name, {})
    records[record["correlation_id"]] = dict(record)
    while len(records) > 20:
        records.pop(next(iter(records)))
    setattr(request.app.state, name, records)
    store = getattr(request.app.state, "auth_diagnostic_store", None)
    if store is not None:
        try:
            store.put(record)
        except (OSError, ValueError, RuntimeError):
            pass  # Diagnostics cannot replace the original authentication result.
    # Emit to the reporting pipe after memory/persist sequence
    try:
        report_diagnostic = make_auth_report_diagnostic(record)
        if report_diagnostic is not None:
            emit_detailed_diagnostic(report_diagnostic)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass  # Reporting failures never alter login outcome


def _get_auth_diagnostic(request: Request, correlation: str, stage: str):
    record = getattr(request.app.state, stage + "_diagnostics", {}).get(correlation)
    if valid_auth_diagnostic(record, correlation, stage):
        return dict(record)
    store = getattr(request.app.state, "auth_diagnostic_store", None)
    return store.get(correlation, stage) if store is not None else None


def _classify_remember_store_failure(error: SecretStoreUnavailable):
    """Classify a secret store initialization failure for remember diagnostics.
    
    Returns (code, reason, error_class, operation, os_status).
    """
    if isinstance(error, SecretReentryRequired):
        return ("credential_locked", "credential_reentry_required",
                "credential_reentry_required", None, None)
    if isinstance(error, SecretInteractionRequired):
        reason = error.reason if error.reason in REMEMBER_REASONS else "keychain_store_unavailable"
        return ("credential_locked", reason, "interaction_required",
                error.operation, error.os_status)
    reason = error.reason if error.reason in REMEMBER_REASONS else "keychain_store_unavailable"
    error_class = error.error_class if error.error_class in REMEMBER_ERROR_CLASSES else "store_unavailable"
    return ("secret_store_unavailable", reason, error_class,
            error.operation, error.os_status)


def restore_failure(request: Request, error: SecretStoreUnavailable) -> HTTPException:
    code = ("credential_locked" if isinstance(error, SecretReentryRequired) else
            "credential_locked" if isinstance(error, SecretInteractionRequired) else "secret_store_unavailable")
    reason = error.reason if error.reason in RESTORE_REASONS else "keychain_store_unavailable"
    correlation_id = secrets.token_hex(16)
    record = {"correlation_id": correlation_id, "stage": "auth_restore", "code": code,
              "reason": reason, "error_class":
              "credential_reentry_required" if isinstance(error, SecretReentryRequired) else
              "secret_interaction_required" if isinstance(error, SecretInteractionRequired) else
              "secret_store_unavailable"}
    if error.os_status is not None:
        record["os_status"] = error.os_status
    _record_auth_diagnostic(request, record)
    detail = {"code": code, "correlation_id": correlation_id}
    if isinstance(error, SecretReentryRequired):
        detail["action"] = "sign_in_again"
    return HTTPException(503, detail=detail)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against hash"""
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('ascii'))
    except (ValueError, TypeError, UnicodeError):
        return False


def get_password_hash(password: str) -> str:
    """Hash password"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('ascii')


def create_access_token(data: dict) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def bind_owner_and_reconcile(request: Request, db: Session, owner_id: int, *,
                             only_if_unbound: bool = False) -> None:
    """Run bounded secure recovery only after Core acknowledged this owner bind."""
    with request.app.state.owner_recovery_lock:
        if (only_if_unbound
                and request.app.state.owner_epoch_provider.snapshot() is not None):
            vault_accounts.attest(request, db, owner_id)
            return
        binding = request.app.state.owner_epoch_provider.bind(owner_id)
        if binding is not None:
            vault_accounts.attest(request, db, owner_id)
    if binding is None:
        return
    coordinator = getattr(request.app.state, "connection_recovery", None)
    if coordinator is None:
        return
    try:
        coordinator.reconcile_owner(db, owner_id, limit=25)
        coordinator.sweep_terminal_receipts(
            db, owner_id, terminal_before=datetime.now(timezone.utc), limit=25)
    except (auth_operations.AuthOperationError, CoreAuthBrokerUnavailable):
        db.rollback()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user"""
    mobile = getattr(request.state, '_gamja_mobile_binding', None)
    if mobile is not None:
        # Only an in-process gateway ASGI scope can carry this object. HTTP
        # headers/body cannot create it; no app JWT is issued to the phone.
        from app.services.mobile_access import MobileBinding
        manager = getattr(request.app.state, 'mobile_access', None)
        if not isinstance(mobile, MobileBinding) or manager is None or mobile.manager is not manager:
            raise HTTPException(401, 'Mobile session required')
        return manager.authorize(mobile, db)
    user = user_from_token(credentials.credentials, db)
    bind_owner_and_reconcile(request, db, user.id, only_if_unbound=True)
    return user


def user_from_token(token: str, db: Session) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload.get("sub"))
        if user_id < 1:
            raise credentials_exception
    except (JWTError, ValueError, TypeError):
        raise credentials_exception
    
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise credentials_exception
    
    return user


@router.post("/register", response_model=UserResponse)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """Register new user"""
    # Check if user exists
    existing_user = db.query(User).filter(
        (User.email == user_data.email) | (User.username == user_data.username)
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email or username already exists"
        )
    
    # Create user
    hashed_password = get_password_hash(user_data.password)
    new_user = User(
        email=user_data.email,
        username=user_data.username,
        hashed_password=hashed_password
    )
    
    db.add(new_user)
    db.flush()
    vault_accounts.add_registered_account(db, new_user.id)
    db.commit()
    db.refresh(new_user)
    
    return new_user


@router.post("/login", response_model=Token)
async def login(user_data: UserLogin, request: Request, db: Session = Depends(get_db)):
    if user_data.preserve_saved_login_for_session:
        raise HTTPException(422, detail={"code": "saved_login_endpoint_required"})
    return await _login(user_data, request, db)


@router.post("/login/session-only", response_model=Token)
async def login_session_only(user_data: UserLogin, request: Request, db: Session = Depends(get_db)):
    if not user_data.preserve_saved_login_for_session:
        raise HTTPException(422, detail={"code": "saved_login_choice_required"})
    return await _login(user_data, request, db)


async def _login(user_data: UserLogin, request: Request, db: Session):
    """Authenticate through the explicitly selected saved-login policy."""
    # Find user
    user = db.query(User).filter(User.email == user_data.email).first()
    
    if not user or not user.is_active or not verify_password(user_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    if user_data.preserve_saved_login_for_session and (user_data.remember_device or user_data.preserve_remembered_login):
        raise HTTPException(422, detail={"code": "saved_login_choice_conflict"})
    with request.app.state.owner_recovery_lock:
        if user_data.preserve_saved_login_for_session:
            row = db.execute(text("SELECT owner_id,secret_ref,revision FROM remembered_login WHERE id=1")).first()
            if row is None:
                raise HTTPException(409, detail={"code": "saved_login_unavailable"})
            if row.owner_id != user.id:
                raise HTTPException(409, detail={"code": "saved_login_owner_mismatch"})
            # Acquire the SQLite writer lock and compare the exact previously read
            # reference/revision before binding the Owner; no secret is queried.
            try:
                matched = db.execute(text("""UPDATE remembered_login SET revision=revision
                    WHERE id=1 AND owner_id=:owner AND secret_ref=:ref AND revision=:revision"""),
                    {"owner": user.id, "ref": row.secret_ref, "revision": row.revision})
                if matched.rowcount != 1:
                    db.rollback()
                    raise HTTPException(409, detail={"code": "saved_login_changed"})
                db.commit()
            except HTTPException:
                raise
            except Exception:
                db.rollback()
                raise HTTPException(409, detail={"code": "saved_login_changed"}) from None
            bind_owner_and_reconcile(request, db, user.id)
        else:
            if user_data.remember_device:
                try:
                    store = secret_store(request)
                except SecretStoreUnavailable as error:
                    # Record diagnostic for store initialization failure
                    correlation_id = secrets.token_hex(16)
                    code, reason, error_class, operation, os_status = _classify_remember_store_failure(error)
                    diagnostic = {
                        "correlation_id": correlation_id,
                        "stage": "auth_remember",
                        "code": code,
                        "reason": reason,
                        "error_class": error_class,
                        "cleanup": "unknown",
                    }
                    if operation is not None:
                        diagnostic["operation"] = operation
                    if os_status is not None:
                        diagnostic["os_status"] = os_status
                    _record_auth_diagnostic(request, diagnostic)
                    raise HTTPException(503, detail={"code": code, "correlation_id": correlation_id}) from None
                try:
                    remember(db, store, user.id)
                except HTTPException as error:
                    # Extract diagnostic from remember() and record it
                    diagnostic = getattr(error, "_remember_diagnostic", None)
                    if diagnostic:
                        _record_auth_diagnostic(request, diagnostic)
                    raise
            elif not user_data.preserve_remembered_login:
                try:
                    store = secret_store(request)
                except SecretStoreUnavailable:
                    store = None
                revoke(db, store)
            bind_owner_and_reconcile(request, db, user.id)
    access_token = create_access_token(data={"sub": user.id})
    
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@router.post("/restore", response_model=Token)
async def restore_login(request: Request, db: Session = Depends(get_db), hint: RestoreHint | None = None):
    expected_owner = hint.expected_owner_id if hint is not None else None
    with request.app.state.owner_recovery_lock:
        binding = request.app.state.owner_epoch_provider.snapshot()
        if expected_owner is not None and binding is not None and binding.owner_id != expected_owner:
            raise HTTPException(401, "No valid automatic login on this device")
        row = db.execute(text("SELECT owner_id, secret_ref, revision FROM remembered_login WHERE id = 1")).first()
        if expected_owner is not None and (row is None or row.owner_id != expected_owner):
            raise HTTPException(401, "No valid automatic login on this device")
        # Legacy manual restore still classifies secure-store failures when no grant exists.
        reference = row.secret_ref if row else current_reference(db)
        revision = row.revision if row else None
        owner_id = row.owner_id if row else None
        try:
            store = secret_store(request)
        except SecretInteractionRequired as error:
            raise restore_failure(request, error) from None
        except SecretStoreUnavailable as error:
            raise restore_failure(request, error) from None
        try:
            credential = store.get_no_ui(reference)
        except SecretInteractionRequired as error:
            raise restore_failure(request, error) from None
        except SecretStoreUnavailable as error:
            raise restore_failure(request, error) from None
        owner = restore_credential(db, credential, expected_owner=owner_id)
        check_row = db.execute(text("SELECT owner_id, secret_ref, revision FROM remembered_login WHERE id = 1")).first()
        if row is None or check_row is None or check_row.secret_ref != reference or check_row.revision != revision or check_row.owner_id != owner_id:
            raise HTTPException(401, "No valid automatic login on this device")
        binding = request.app.state.owner_epoch_provider.snapshot()
        if expected_owner is not None and binding is not None:
            if binding.owner_id != owner.id:
                raise HTTPException(401, "No valid automatic login on this device")
            # Continuity must not rotate a live Core epoch or clear its credential cache.
        elif (expected_owner is None and binding is not None and binding.owner_id == owner.id
              and isinstance(binding.owner_epoch,str) and re.fullmatch(r'[a-f0-9]{64}',binding.owner_epoch)
              and request.app.state.owner_epoch_provider.current(owner.id) == binding.owner_epoch):
            # A verified remembered login can reuse the same currently live Core binding.
            pass
        else:
            bind_owner_and_reconcile(request, db, owner.id)
        vault_accounts.attest(request, db, owner.id)
    return {"access_token": create_access_token({"sub": owner.id}), "token_type": "bearer"}


@router.get("/restore/diagnostics/{correlation_id}")
async def restore_diagnostic(request: Request, correlation_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", correlation_id):
        raise HTTPException(404, "Restore diagnostic not found")
    record = _get_auth_diagnostic(request, correlation_id, "auth_restore")
    if record is None:
        raise HTTPException(404, "Restore diagnostic not found")
    return record


@router.get("/remember/diagnostics/{correlation_id}")
async def remember_diagnostic(request: Request, correlation_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", correlation_id):
        raise HTTPException(404, "Remember diagnostic not found")
    record = _get_auth_diagnostic(request, correlation_id, "auth_remember")
    if record is None:
        raise HTTPException(404, "Remember diagnostic not found")
    return record


@router.post("/restore/interactive", status_code=202)
async def start_interactive_restore(request: Request, db: Session = Depends(get_db)):
    try:
        store = secret_store(request)
    except SecretStoreUnavailable:
        raise HTTPException(503, "Automatic login secure store unavailable") from None
    if not getattr(store, "supports_interactive_unlock", True):
        return {"state": "reentry_required", "action": "sign_in_again"}
    cache = getattr(request.app.state, 'credential_cache', None)
    if cache is None:
        raise HTTPException(503, "Automatic login secure store unavailable")
    unlock_id = getattr(request.app.state, 'login_unlock_id', None)
    if unlock_id is None:
        unlock_id = secrets.token_urlsafe(24)
        request.app.state.login_unlock_id = unlock_id
    key = ("remembered-login", unlock_id)
    return {"state": cache.start(key, store, current_reference(db)), "unlock_id": unlock_id}


@router.get("/restore/interactive")
async def finish_interactive_restore(request: Request, unlock_id: str = Query(min_length=20, max_length=128),
                                     db: Session = Depends(get_db)):
    if unlock_id != getattr(request.app.state, 'login_unlock_id', None):
        raise HTTPException(404, 'Saved-login request not found')
    cache = getattr(request.app.state, 'credential_cache', None)
    key = ("remembered-login", unlock_id)
    if cache is None or cache.status(key) != 'ready':
        return {"state": 'pending' if cache and cache.status(key) == 'pending' else 'locked'}
    credential = cache.get(key)
    cache.clear(key)
    del request.app.state.login_unlock_id
    owner = restore_credential(db, credential)
    bind_owner_and_reconcile(request, db, owner.id)
    return {"state": 'ready', "access_token": create_access_token({"sub": owner.id}), "token_type": "bearer"}


@router.post("/logout", status_code=204)
async def logout_login(request: Request, db: Session = Depends(get_db)):
    with request.app.state.owner_recovery_lock:
        expected_owner = request.app.state.owner_epoch_provider.snapshot()
        try:
            try:
                store = secret_store(request)
            except SecretStoreUnavailable:
                store = None
            revoke(db, store)
        finally:
            cache = getattr(request.app.state, 'credential_cache', None)
            if cache:
                cache.clear_all()
            if hasattr(request.app.state, 'login_unlock_id'):
                del request.app.state.login_unlock_id
            try:
                request.app.state.owner_epoch_provider.compare_and_unbind(expected_owner)
            except CoreAuthBrokerUnavailable:
                pass
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    return current_user
