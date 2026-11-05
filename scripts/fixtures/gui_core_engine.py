#!/usr/bin/env python3
"""Operator-only SOURCE engine for the packaged GUI/core lifetime gate.

Never bundled or enabled by the installed runtime. Invoke through the existing
core --engine/--engine-arg contract. All domain/auth/database/scheduler routes
remain canonical; only OS credentials, vendor transport and local effects are
replaced in this separately labeled fixture process.
"""
import asyncio
import math
import os
from pathlib import Path
import secrets
import sys


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.no_ui_reads = 0

    def put(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def get_no_ui(self, key):
        self.no_ui_reads += 1
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)


class NoEffectsExecutor:
    def __init__(self, *args, **kwargs):
        pass

    async def __call__(self, *args):
        raise RuntimeError('fixture_executor_denied')

    async def close(self):
        pass

    async def stop_room(self, *args):
        pass

    async def stop_grant(self, *args):
        pass

    async def release_desktop_turn(self, *args):
        pass


def install_fixture(*, delay_seconds=20, patch=setattr):
    if not isinstance(delay_seconds, (int, float)) or not math.isfinite(delay_seconds) or not 0 < delay_seconds <= 60:
        raise ValueError('invalid_fixture_delay')
    import httpx
    import app.main as main
    from app.services.providers import ProviderResult
    from app.services.secrets import PlatformSecretStore
    canonical_create = main.create_app
    canonical_scheduler = main.Scheduler

    def deny_native_store(self):
        # Includes inherited LoginSecretStore.__init__: fail rather than touch
        # Owner Keychain if either separately injected app-state store is lost.
        raise RuntimeError('fixture_native_store_denied')

    class FixtureScheduler(canonical_scheduler):
        def __init__(self, sessions, hub, state):
            super().__init__(sessions, hub, state)
            async def provider(request):
                state.fixture_provider_calls += 1
                await asyncio.sleep(delay_seconds)
                return ProviderResult(text='Synthetic local fixture completed.')
            self.provider = provider

    def create_fixture_app(*args, **kwargs):
        app = canonical_create(*args, **kwargs)
        # These must exist BEFORE lifespan starts or automatic /auth/restore.
        app.state.secret_store = MemoryStore()
        app.state.login_secret_store = MemoryStore()
        app.state.fixture_provider_calls = 0
        app.state.fixture_transport_calls = 0
        def wire(request):
            app.state.fixture_transport_calls += 1
            # Vendor-looking addresses never leave this in-memory transport.
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Synthetic probe OK'}}]})
        app.state.connection_transport = httpx.MockTransport(wire)
        return app

    patch(PlatformSecretStore, '__init__', deny_native_store)
    patch(main, 'Scheduler', FixtureScheduler)
    patch(main, 'LocalExecutor', NoEffectsExecutor)
    patch(main, 'create_app', create_fixture_app)


def main():
    backend = Path(__file__).resolve().parents[2] / 'backend'
    sys.path.insert(0, str(backend))
    if len(sys.argv) > 1 and sys.argv[1] == '--cli':
        # The public Rust adapter supplies --capability-file explicitly. This
        # client has no engine-only environment and uses the normal CLI parser.
        from app.owner_cli import main as cli_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        cli_main()
        return
    # Import settings only after entering the explicit private app-data directory;
    # never load a repository/Owner .env. Preserve inherited HOME unchanged.
    data = Path(os.environ['GAMJA_APP_DATA_DIR'])
    if not data.is_absolute() or data.is_symlink() or not data.is_dir():
        raise RuntimeError('fixture_app_data_invalid')
    os.chdir(data)
    if (data / '.env').exists():
        raise RuntimeError('fixture_dotenv_denied')
    os.environ['DATABASE_URL'] = f"sqlite:///{data / 'gamja_bot.db'}"
    os.environ['SECRET_KEY'] = secrets.token_urlsafe(32)
    install_fixture()
    import run
    run.main()


if __name__ == '__main__':
    main()
