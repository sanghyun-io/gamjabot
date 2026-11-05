# -*- mode: python ; coding: utf-8 -*-
"""One target-native sidecar recipe. Run with the target's Python and wheels."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=collect_data_files("playwright", includes=["driver/**"]),
    hiddenimports=(collect_submodules("asyncio")
                   + collect_submodules("app.core.migrations")
                   + ["keyring.backends.macOS", "keyring.backends.Windows",
                      "PIL.PngImagePlugin", "PIL._imaging", "playwright.async_api"]),
    hookspath=[],
    hooksconfig={},
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="backend", console=True, strip=False, upx=False,
    bootloader_ignore_signals=False,
)
