"""
Gamja Bot Backend Entry Point
"""
import os
import asyncio
import platform
import secrets
import signal
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from app.services.diagnostics import emit_diagnostic_failure


ProviderRecoveryMode = Literal["legacy", "core_managed_recovery_required"]


def provider_recovery_mode(
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    machine: str | None = None,
) -> ProviderRecoveryMode:
    """Classify the trusted launcher before inherited descriptors are scrubbed."""
    values = os.environ if environment is None else environment
    inherited_private_channel = any(
        name in values for name in (
            "GAMJA_CREDENTIAL_FD", "GAMJA_AUTH_BROKER_FD", "GAMJA_PROVIDER_FD",
            "GAMJA_UPGRADE_FD", "GAMJA_MCP_FD",
        )
    )
    owned_apple_silicon_engine = (
        values.get("GAMJA_DESKTOP_OWNER_STDIN") == "1"
        and (sys.platform if platform_name is None else platform_name) == "darwin"
        and (platform.machine() if machine is None else machine).lower() == "arm64"
    )
    if inherited_private_channel or owned_apple_silicon_engine:
        return "core_managed_recovery_required"
    return "legacy"


def start_owner_watchdog() -> None:
    """The core (or legacy desktop) owns stdin; EOF ends the owned engine group."""
    def watch() -> None:
        try:
            group_line = bytearray()
            while len(group_line) < 20:
                byte = os.read(0, 1)
                if byte == b"\n":
                    break
                if not byte or not byte.isdigit():
                    os._exit(1)
                group_line.extend(byte)
            else:
                os._exit(1)
            expected_owner = int(group_line)
            if os.name == "posix" and os.getpgrp() != expected_owner:
                os._exit(1)
            while os.read(0, 1):
                pass
        except (OSError, ValueError):
            os._exit(1)
        if os.name == "posix":
            # The host handshake names the dedicated group created at spawn.
            # Kill immediately: TERM could end this watchdog before a resistant
            # launcher receives a later KILL. Normal host shutdown is graceful.
            if os.getpgrp() == expected_owner:
                os.killpg(expected_owner, signal.SIGKILL)
        os._exit(1)

    threading.Thread(target=watch, name="core-owner-lifetime", daemon=True).start()


async def package_smoke() -> None:
    """Check the frozen Playwright driver and installed Chromium without network access."""
    from playwright.async_api import async_playwright

    browsers = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if not browsers.is_dir():
        raise RuntimeError("Bundled browser resource is missing")
    with tempfile.TemporaryDirectory(prefix="gamja-package-smoke-") as profile:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(profile, headless=True)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("data:text/html,<title>Gamja fixture</title><p id='proof'>local only</p>")
                if await page.locator("#proof").inner_text() != "local only":
                    raise RuntimeError("Bundled browser did not render the local fixture")
            finally:
                await context.close()
    print("Bundled Playwright driver and Chromium passed local fixture")


def main() -> None:
    recovery_mode = provider_recovery_mode()
    if os.environ.get("GAMJA_DESKTOP_OWNER_STDIN") == "1":
        start_owner_watchdog()
    try:
        os.umask(0o077)
        secret = os.environ["GAMJA_LOCAL_SESSION_TOKEN"]
        if len(secret) < 32:
            raise RuntimeError("Missing desktop session credential")
        app_data = Path(os.environ["GAMJA_APP_DATA_DIR"]).resolve()
        app_data.mkdir(parents=True, exist_ok=True)
        port = int(os.environ["GAMJA_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("Invalid service port")
        os.chdir(app_data)
        os.environ["DATABASE_URL"] = f"sqlite:///{app_data / 'gamja_bot.db'}"
        os.environ["SECRET_KEY"] = secrets.token_urlsafe(32)

        from app.main import RuntimeConfig, create_app
        import uvicorn

        app = create_app(runtime_config=RuntimeConfig.from_environment(
            provider_recovery_mode=recovery_mode,
        ))
    except Exception:
        emit_diagnostic_failure("backend_init_failed", "startup")
        raise
    # Settings and app state now own these credentials. Browser/Playwright
    # children launched later must not inherit them from the process env.
    for name in ("GAMJA_LOCAL_SESSION_TOKEN", "GAMJA_DESKTOP_IPC_KEY", "GAMJA_DESKTOP_IPC_ENDPOINT",
                 "GAMJA_DESKTOP_OWNER_STDIN", "GAMJA_CLI_CAPABILITY_HASH", "GAMJA_CREDENTIAL_FD",
                 "GAMJA_AUTH_BROKER_FD", "GAMJA_PROVIDER_FD", "GAMJA_UPGRADE_FD", "GAMJA_MCP_FD", "SECRET_KEY"):
        os.environ.pop(name, None)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, reload=False, access_log=False)
    except Exception:
        emit_diagnostic_failure("backend_runtime_failed", "runtime")
        raise

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--extract-library-office-docx", "--extract-library-office-xlsx", "--extract-library-office-pptx"):
        from app.services.library_file_text_worker import main as file_text_main
        sys.exit(file_text_main(sys.argv[1].rsplit("-",1)[-1]))
    if len(sys.argv) == 2 and sys.argv[1] == "--extract-library-pdf-ocr":
        from app.services.library_pdf_ocr import main as pdf_ocr_main
        sys.exit(pdf_ocr_main())
    if len(sys.argv) == 2 and sys.argv[1] == "--extract-library-file":
        from app.services.library_file_text_worker import main as file_text_main
        sys.exit(file_text_main())
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from app.owner_cli import main as owner_cli_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        owner_cli_main()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--process-guardian":
        from app.services.tools.guardian import main as guardian_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        sys.exit(guardian_main())
    if len(sys.argv) > 1 and sys.argv[1] == "--package-smoke":
        asyncio.run(package_smoke())
        sys.exit(0)
    main()
