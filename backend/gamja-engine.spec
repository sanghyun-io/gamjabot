# -*- mode: python ; coding: utf-8 -*-
"""Standalone private engine recipe; no desktop or Tauri resources. Run with the target's Python and wheels."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

scripts = Path(SPECPATH).parent / "scripts"
if scripts.is_symlink() or not scripts.is_dir():
    raise ValueError("canonical build scripts directory is unavailable")
playwright_payload = collect_data_files("playwright", includes=["driver/**"])
if sys.platform == "darwin":
    sys.path.insert(0, str(scripts))
    from macos_engine_constraints import (  # noqa: E402
        finalize_pyinstaller_constraint_hook,
        install_pyinstaller_constraint_hook,
        partition_native_payload,
        playwright_helper_destinations,
    )
    playwright_binaries, playwright_datas = partition_native_payload(playwright_payload)
    playwright_helpers = playwright_helper_destinations(playwright_binaries)
else:
    playwright_binaries, playwright_datas = [], playwright_payload

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=playwright_binaries,
    datas=[*playwright_datas, *copy_metadata("pypdf")],
    hiddenimports=(collect_submodules("asyncio")
                   + collect_submodules("app.core.migrations")
                   + ["keyring.backends.macOS", "keyring.backends.Windows",
                      "PIL.PngImagePlugin", "PIL._imaging", "playwright.async_api"]),
    hookspath=[],
    hooksconfig={},
    excludes=[],
    noarchive=False,
)
if sys.platform == "darwin":
    install_pyinstaller_constraint_hook(a.binaries, playwright_helpers)
try:
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="gamja-engine", console=True, strip=False, upx=False,
        bootloader_ignore_signals=False,
    )
finally:
    if sys.platform == "darwin":
        finalize_pyinstaller_constraint_hook()
