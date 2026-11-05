from pathlib import Path
"""
Gamja Bot - Main Application
Grok-style multi-agent system with FastAPI backend
"""
import os
import re
import asyncio
import platform
import sys
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4
from typing import Literal
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager, nullcontext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

from app.core.config import settings
import app.models  # Register mappings before opening the database.
from app.core.migrate import migrate_database
from app.api import api_router
from app.core.local_auth import ALLOWED_ORIGINS, require_local_session
from app.services.event_hub import EventHub
from app.services.scheduler import Scheduler
from app.services.room_plans import RoomPlans
from app.services.tool_broker import ToolBroker, TOOL_SCHEMAS
from app.services.tools import LocalExecutor
from app.services.tools.desktop import BackendStartupLock
from app.services.diagnostics import emit_diagnostic_failure
from app.services.secrets import CoreCredentialClient, CoreSecretStore, SessionCredentialCache, SecretStoreUnavailable
from app.services.core_auth_broker import (
    CoreAuthBrokerClient, CoreAuthBrokerUnavailable, TrustedOwnerEpochProvider,
)
from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable
from app.services.connection_recovery import ConnectionRecoveryCoordinator
from app.services import provider_catalog
from app.services.auth_diagnostics import AuthDiagnosticStore
from app.services.secrets import consume_inherited_socket
from app.services.core_upgrade_login import consume_upgrade_channel


ProviderRecoveryMode = Literal["legacy", "core_managed_recovery_required"]


def _personal_agent_core(config, credential_client, auth_client) -> bool:
    """Bind personal use to native private channels and both local transports."""
    return (isinstance(credential_client, CoreCredentialClient)
            and isinstance(auth_client, CoreAuthBrokerClient)
            and config.credential_fd is not None and config.auth_broker_fd is not None
            and isinstance(config.local_session_token, str) and len(config.local_session_token) >= 32
            and isinstance(config.cli_capability_hash, str) and len(config.cli_capability_hash) == 64
            and all(character in '0123456789abcdef' for character in config.cli_capability_hash))


@dataclass(frozen=True)
class RuntimeConfig:
    """Typed launch boundary; transport credentials never become user JWT keys."""
    database_url: str
    local_session_token: str | None = None
    cli_capability_hash: str | None = None
    native_endpoint: str | None = None
    native_key: str | None = None
    credential_fd: int | None = None
    auth_broker_fd: int | None = None
    provider_fd: int | None = None
    mcp_fd: int | None = None
    upgrade_fd: int | None = None
    provider_recovery_mode: ProviderRecoveryMode = "legacy"
    auth_diagnostic_dir: str | None = None
    publisher_fd: int | None = None
    team_trust_profiles: tuple = ()

    def __post_init__(self) -> None:
        if self.provider_recovery_mode not in {
            "legacy", "core_managed_recovery_required",
        }:
            raise ValueError("Invalid provider recovery mode")
        descriptors = (self.credential_fd, self.auth_broker_fd, self.provider_fd, self.upgrade_fd, self.mcp_fd, self.publisher_fd)
        if any(value is not None and (type(value) is not int or not 3 <= value < 1_000_000)
               for value in descriptors):
            raise ValueError("Invalid private descriptor")
        present = [value for value in descriptors if value is not None]
        if self.mcp_fd is not None and self.auth_broker_fd is None:
            raise ValueError("MCP channel requires Core authentication")
        if self.publisher_fd is not None and (self.auth_broker_fd is None or self.credential_fd is None):
            raise ValueError("Publisher channel requires Core authentication")
        if len(present) != len(set(present)):
            raise ValueError("Private descriptors must be distinct")
        if self.upgrade_fd is not None and (
            sys.platform != "darwin" or platform.machine().lower() != "arm64"
            or self.provider_recovery_mode != "core_managed_recovery_required"
            or any(fd is None for fd in descriptors[:3])
        ):
            raise ValueError("Invalid private upgrade launch")

    @classmethod
    def from_environment(
        cls,
        database_url: str | None = None,
        *,
        provider_recovery_mode: ProviderRecoveryMode = "legacy",
    ) -> "RuntimeConfig":
        """Compatibility with the legacy desktop and development runner."""
        credential_fd = os.environ.get("GAMJA_CREDENTIAL_FD")
        auth_broker_fd = os.environ.get("GAMJA_AUTH_BROKER_FD")
        provider_fd = os.environ.get("GAMJA_PROVIDER_FD")
        upgrade_fd = os.environ.get("GAMJA_UPGRADE_FD")
        mcp_fd = os.environ.get("GAMJA_MCP_FD")
        publisher_fd = os.environ.get("GAMJA_TEMPLATE_PUBLISHER_FD")
        from app.services.team_trust_file import load_team_trust_file
        team_profiles=load_team_trust_file(os.environ.get("GAMJA_APP_DATA_DIR")) if database_url is None else ()
        return cls(
            team_trust_profiles=team_profiles,
            database_url=database_url or settings.DATABASE_URL,
            local_session_token=os.environ.get("GAMJA_LOCAL_SESSION_TOKEN"),
            cli_capability_hash=os.environ.get("GAMJA_CLI_CAPABILITY_HASH"),
            native_endpoint=os.environ.get("GAMJA_DESKTOP_IPC_ENDPOINT"),
            native_key=os.environ.get("GAMJA_DESKTOP_IPC_KEY"),
            credential_fd=_parse_private_descriptor(credential_fd, "credential"),
            auth_broker_fd=_parse_private_descriptor(auth_broker_fd, "auth broker"),
            provider_fd=_parse_private_descriptor(provider_fd, "provider"),
            upgrade_fd=_parse_private_descriptor(upgrade_fd, "upgrade"),
            mcp_fd=_parse_private_descriptor(mcp_fd, "mcp"),
            publisher_fd=_parse_private_descriptor(publisher_fd, "publisher"),
            provider_recovery_mode=provider_recovery_mode,
            auth_diagnostic_dir=(os.environ.get("GAMJA_APP_DATA_DIR") if database_url is None else None),
        )


def _parse_private_descriptor(value: str | None, label: str) -> int | None:
    if value is None:
        return None
    if (not value or len(value) > 6 or not value.isascii() or not value.isdigit()):
        raise ValueError(f"Invalid {label} descriptor")
    descriptor = int(value)
    if not 3 <= descriptor < 1_000_000:
        raise ValueError(f"Invalid {label} descriptor")
    return descriptor


def create_app(database_url: str | None = None, *, runtime_config: RuntimeConfig | None = None) -> FastAPI:
    if database_url is not None and runtime_config is not None:
        raise ValueError("Use one runtime configuration source")
    config = runtime_config or RuntimeConfig.from_environment(database_url)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        url = config.database_url
        engine = create_engine(
            url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
        )
        database_path = engine.url.database if engine.url.database not in (None, ":memory:") else None
        with BackendStartupLock(database_path) if database_path else nullcontext():
            try:
                app.state.shared_clear_instance = str(uuid4())
                app.state.shared_clear_fence_ready = bool(database_path)
                app.state.shared_clear_live = set()
                migrate_database(engine)
                app.state.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
                app.state.credential_cache = SessionCredentialCache()
                app.state.owner_epoch_provider.set_credential_cache(app.state.credential_cache)
                if upgrade_channel is not None:
                    with app.state.session_factory() as db:
                        consume_upgrade_channel(
                            db, app.state.login_secret_store, upgrade_channel,
                            app.state.auth_broker_client.current_generation_fingerprint,
                            diagnostics=app.state.auth_diagnostic_store,
                        )
                if hasattr(app.state, "auth_broker_client"):
                    app.state.connection_recovery = ConnectionRecoveryCoordinator(
                        broker=app.state.auth_broker_client,
                        owner_epochs=app.state.owner_epoch_provider,
                        credential_cache=app.state.credential_cache,
                        auth_method_availability=(
                            lambda provider_id, method_id,
                                   available=runtime_auth_methods:
                            (provider_id, method_id) in available
                        ),
                    )
                app.state.event_hub = EventHub()
                app.state.scheduler = Scheduler(app.state.session_factory, app.state.event_hub, app.state)
                app.state.local_executor = LocalExecutor(app.state.session_factory, database_path=database_path,
                    native_endpoint=app.state.native_endpoint, native_key=app.state.native_key,
                    owner_epochs=(app.state.owner_epoch_provider if auth_broker_client is not None else None))
                app.state.scheduler.broker = ToolBroker(app.state.session_factory, app.state.local_executor,
                    owner_epochs=(app.state.owner_epoch_provider if auth_broker_client is not None else None))
                if database_path:
                    app.state.room_plans = RoomPlans(database_path,app.state.session_factory,app.state.event_hub)
                    await app.state.room_plans.start()
                app.state.model_usage = None
                if database_path:
                    from app.services import model_usage
                    try:
                        app.state.model_usage = model_usage.install(
                            Path(database_path).resolve().with_name('model-usage.db'), app.state.session_factory)
                    except Exception:
                        # Recording failure must not trigger an extra paid model call.
                        app.state.model_usage = None
                from app.services import chatgpt_oauth
                app.state.chatgpt_oauth = None
                if database_path:
                    try:
                        app.state.chatgpt_oauth = chatgpt_oauth.install(
                            Path(database_path).resolve().with_name('chatgpt-oauth'), app.state.session_factory,
                            getattr(app.state, 'owner_epoch_provider', None))
                    except (OSError, ValueError):
                        # An unavailable new OAuth store must not break existing connections.
                        app.state.chatgpt_oauth = None
                app.state.mcp_client = None
                if config.mcp_fd is not None:
                    from app.services.mcp_client import MCPClient
                    try:
                        candidate=await MCPClient.from_fd(config.mcp_fd)
                        expected=getattr(app.state.auth_broker_client,'current_generation_fingerprint',None)
                        if candidate.core_epoch!=expected:
                            await candidate.close()
                        else:app.state.mcp_client=candidate
                    except Exception:app.state.mcp_client=None
                app.state.shared_connector_runtime_ready = False
                if app.state.mcp_client is not None:
                    try:
                        shared_capability=await asyncio.wait_for(app.state.mcp_client.shared_capabilities({'version':1}),3)
                        app.state.shared_connector_runtime_ready=(shared_capability.get('state')=='completed' and shared_capability.get('result')=={'version':1,'shared_recipient_mcp':True})
                    except Exception:pass
                app.state.local_executor.speech_client = app.state.mcp_client
                app.state.local_executor.speech_available = False
                app.state.local_executor.speech_voices = []
                if app.state.mcp_client is not None:
                    try:
                        speech = await asyncio.wait_for(app.state.mcp_client.speech_capabilities({'version':1}), 3)
                        data = speech.get('result') if speech.get('state') == 'completed' else None
                        voices = data.get('voices') if isinstance(data, dict) else None
                        valid = (isinstance(voices, list) and 1 <= len(voices) <= 128 and
                            all(isinstance(v,dict) and set(v)=={'id','locale'} and
                                isinstance(v['id'],str) and 1<=len(v['id'])<=200 and v['id'].isprintable() and
                                isinstance(v['locale'],str) and 1<=len(v['locale'])<=64 and v['locale'].isprintable()
                                for v in voices) and len({(v['id'],v['locale']) for v in voices})==len(voices))
                        if valid and data.get('available') is True and data.get('format') == 'pcm_s16le_22050_mono_wav':
                            import json
                            selected = []
                            ordered = sorted(voices, key=lambda voice: (
                                0 if voice['locale'].lower().startswith('ko') else
                                1 if voice['locale'].lower().startswith('en') else 2,
                                voice['locale'], voice['id']))
                            distinct = []
                            seen_locales = set()
                            for voice in ordered:
                                locale = voice['locale'].lower()
                                if locale not in seen_locales:
                                    distinct.append(voice); seen_locales.add(locale)
                            ordered = distinct + [voice for voice in ordered if voice not in distinct]
                            for voice in ordered:
                                candidate = [*selected, voice]
                                if len(candidate) > 16 or len(json.dumps(candidate, ensure_ascii=True, separators=(',', ':')).encode()) > 4096:
                                    break
                                selected = candidate
                            app.state.local_executor.speech_voices = selected
                            app.state.local_executor.speech_available = bool(selected)
                    except Exception:
                        pass
                if app.state.local_executor.speech_available:
                    import json
                    speech_tool = next(item for item in TOOL_SCHEMAS if item.get('name') == 'synthesize_voice_memo')
                    speech_tool = {**speech_tool, 'description':speech_tool['description'] +
                        ' Select an exact id and locale from this bounded list of generic system voices; other voices and languages are unavailable for this request: ' +
                        json.dumps(app.state.local_executor.speech_voices, ensure_ascii=True, separators=(',', ':'))}
                    app.state.scheduler.broker.tool_schemas = [*app.state.scheduler.broker.tool_schemas, speech_tool]
                from app.services import slack as slack_service
                with app.state.scheduler.sessions() as slack_db:
                    slack_service.reconcile_startup(slack_db)
                await app.state.scheduler.start()
                app.state.slack_locks = {}
                app.state.slack_task = asyncio.create_task(slack_service.loop(app))
                app.state.mobile_access = None
                if database_path and config.local_session_token:
                    from app.services.mobile_access import MobileAccess
                    try:
                        app.state.mobile_access = MobileAccess(Path(database_path).resolve().with_name('mobile-access'), app)
                        await app.state.mobile_access.start()
                    except (OSError, ValueError, RuntimeError):
                        if app.state.mobile_access is not None:
                            await app.state.mobile_access.close()
                        # Retain an initialized manager so the Owner can retry
                        # or disable a listener whose port was unavailable.
                yield
            finally:
                if getattr(app.state, 'team_connection', None) is not None:
                    await asyncio.to_thread(app.state.team_connection.close)
                    app.state.team_connection = None
                if getattr(app.state, 'mobile_access', None) is not None:
                    await app.state.mobile_access.close()
                if getattr(app.state, 'chatgpt_oauth', None) is not None:
                    await asyncio.to_thread(app.state.chatgpt_oauth.close)
                if upgrade_channel is not None:
                    upgrade_channel.close()
                if hasattr(app.state, "room_plans"):
                    await app.state.room_plans.close()
                if getattr(app.state, "slack_task", None) is not None:
                    app.state.slack_task.cancel()
                    await asyncio.gather(app.state.slack_task, return_exceptions=True)
                if hasattr(app.state, "scheduler"):
                    await app.state.scheduler.close()
                tasks=getattr(app.state,'mcp_discovery_tasks',set())
                for task in tuple(tasks):task.cancel()
                if tasks:await asyncio.gather(*tuple(tasks),return_exceptions=True)
                if getattr(app.state,'mcp_client',None) is not None:
                    await app.state.mcp_client.close()
                if hasattr(app.state, "local_executor"):
                    await app.state.local_executor.close()
                if getattr(app.state, 'model_usage', None) is not None:
                    from app.services import model_usage
                    await model_usage.close()
                if hasattr(app.state, "credential_cache"):
                    app.state.credential_cache.close()
                if hasattr(app.state, "provider_client"):
                    app.state.provider_client.close()
                if hasattr(app.state, "auth_broker_client"):
                    app.state.auth_broker_client.close()
                if hasattr(app.state, "template_publisher_credential_client"):
                    app.state.template_publisher_credential_client.close()
                if hasattr(app.state, "credential_client"):
                    app.state.credential_client.close()
                engine.dispose()

    app = FastAPI(
        title=settings.APP_NAME,
        description="Grok-style multi-agent system API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.local_session_token = config.local_session_token
    app.state.cli_capability_hash = config.cli_capability_hash
    app.state.native_endpoint = config.native_endpoint
    app.state.native_key = config.native_key
    app.state.provider_recovery_mode = config.provider_recovery_mode
    app.state.auth_diagnostic_store = AuthDiagnosticStore(config.auth_diagnostic_dir)
    app.state.owner_recovery_lock = RLock()
    app.state.team_connection = None
    from app.services.team_trust_profiles import build_registry
    app.state.team_trust_profiles = build_registry(config.team_trust_profiles)
    credential_client = None
    publisher_client = None
    auth_broker_client = None
    provider_client = None
    upgrade_channel = None
    opened_clients = []
    try:
        if config.upgrade_fd is not None:
            upgrade_channel = consume_inherited_socket(config.upgrade_fd)
            opened_clients.append(upgrade_channel)
        if config.credential_fd is not None:
            credential_client = CoreCredentialClient.from_fd(config.credential_fd)
            opened_clients.append(credential_client)
        if config.publisher_fd is not None:
            try:
                publisher_client = CoreCredentialClient.from_fd(config.publisher_fd)
                opened_clients.append(publisher_client)
            except SecretStoreUnavailable:
                publisher_client = None
        if config.auth_broker_fd is not None:
            try:
                auth_broker_client = CoreAuthBrokerClient.from_fd(config.auth_broker_fd)
                opened_clients.append(auth_broker_client)
            except CoreAuthBrokerUnavailable:
                if (config.provider_recovery_mode != "core_managed_recovery_required"
                        or config.provider_fd is not None):
                    raise
        if config.provider_fd is not None:
            if auth_broker_client is None:
                raise CoreProviderUnavailable("provider_auth_attachment_required")
            provider_client = CoreProviderClient.from_fd(
                config.provider_fd,
                expected_generation_fingerprint=(
                    auth_broker_client.current_generation_fingerprint
                ),
            )
            opened_clients.append(provider_client)
    except Exception:
        for client in reversed(opened_clients):
            client.close()
        raise
    runtime_auth_methods = (
        provider_catalog.RUNTIME_AUTH_METHODS
        if auth_broker_client is not None and provider_client is not None
        else frozenset()
    )
    try:
        if credential_client is not None:
            app.state.credential_client = credential_client
            app.state.secret_store = CoreSecretStore(
                credential_client, "provider", personal_agent_runtime=_personal_agent_core(
                    config, credential_client, auth_broker_client), auth_broker=auth_broker_client)
            app.state.login_secret_store = CoreSecretStore(
                credential_client, "remembered_login")
        if publisher_client is not None:
            app.state.template_publisher_secret_store = CoreSecretStore(
                publisher_client, "template_publisher")
            app.state.template_publisher_credential_client = publisher_client
        if auth_broker_client is not None:
            app.state.auth_broker_client = auth_broker_client
        if provider_client is not None:
            app.state.provider_client = provider_client
        app.state.owner_epoch_provider = TrustedOwnerEpochProvider(auth_broker_client)
    except Exception:
        for client in reversed(opened_clients):
            client.close()
        raise

    @app.exception_handler(RequestValidationError)
    async def safe_connection_validation(request: Request, error: RequestValidationError):
        if request.url.path.startswith('/api/v1/team-connection'):
            return JSONResponse({'detail': {'code': 'team_input_invalid'}}, status_code=422)
        if request.url.path.startswith('/api/v1/connections'):
            return JSONResponse({'detail': {'code': 'invalid_request', 'message': 'Invalid connection input.'}}, status_code=422)
        if request.url.path.startswith('/api/v1/workspace'):
            return JSONResponse({'detail': {'code': 'workspace_invalid_blueprint'}}, status_code=422)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(Exception)
    async def unexpected_request_failure(request: Request, error: Exception):
        emit_diagnostic_failure("backend_runtime_failed", "runtime")
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["x-gamja-session", "authorization", "content-type", "idempotency-key",
                       "x-upload-filename", "x-upload-sha256"],
        expose_headers=["X-Gamja-Scope-Stop-Id", "X-Captured-At", "X-Browser-Profile-Id",
                        "X-Browser-Profile-Revision", "X-Browser-Context-Id"],
    )

    @app.middleware("http")
    async def local_session_boundary(request: Request, call_next):
        # CORS preflight discloses no route data; the actual request still needs the token.
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            if request.headers.get("origin") not in ALLOWED_ORIGINS:
                return JSONResponse({"detail": "Origin denied"}, status_code=403)
            return await call_next(request)
        if request.method == 'POST' and re.fullmatch(r'/api/v1/routine-webhooks/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',request.url.path):
            origin=request.headers.get('origin')
            if origin and origin not in ALLOWED_ORIGINS:return JSONResponse({'detail':'Origin denied'},status_code=403)
            return await call_next(request)
        try:
            require_local_session(request)
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        return await call_next(request)
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/")
    async def root():
        return {"message": "Gamja Bot API", "version": "1.0.0", "docs": "/docs"}

    @app.get("/health")
    async def health_check():
        return {"status": "healthy"}

    return app


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
