"""Mac ARM GUI assembly without Tauri's redundant Core resource copies.

The normal recipe still verifies/stages its input and restores the canonical Core
with its original verifier. This adapter only omits the generic resource copies.
"""
from copy import deepcopy
import os
from pathlib import Path

# Planned additional peak, not a measured peak or an atomic reservation.
# At least the prior866841028-byte release/gamja-core staging is eliminated.
# Reduce the prior3.5GiB plan by only0.75GiB; retain its remaining headroom.
RAW_PEAK_GIB = 2.75
PROFILE = 'deferred_core_mac_gui'


def deferred_core_config(config):
    value = deepcopy(config)
    resources = value.get('bundle', {}).get('resources')
    if not isinstance(resources, dict) or resources.get('resources/gamja-core/') != 'gamja-core/':
        raise ValueError('expected canonical Core resource mapping')
    if resources.get('../../scripts/Diagnostics Help.txt') != 'Diagnostics Help.txt':
        raise ValueError('expected diagnostics resource mapping')
    # Tauri config uses merge-patch null, as the recipe already does for browsers.
    resources['resources/gamja-core/'] = None
    return value


def restore_deferred_core(source, app, bundle, target, *, restore):
    app, bundle = Path(app), Path(bundle)
    if (target != 'aarch64-apple-darwin' or not bundle.is_absolute()
            or bundle.parts[-3:] != (target, 'release', 'bundle')
            or app != bundle / 'macos/Gamja Bot.app' or bundle.resolve() != bundle):
        raise ValueError('expected owned Mac ARM bundle')
    for directory in [bundle, bundle / 'macos', app, app / 'Contents', app / 'Contents/Resources']:
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.geteuid():
            raise ValueError('expected real build-owned resource directory')
    intermediate = bundle.parent / 'gamja-core'
    destination = app / 'Contents/Resources/gamja-core'
    if intermediate.exists() or intermediate.is_symlink() or destination.exists() or destination.is_symlink():
        raise RuntimeError('generic Core copy unexpectedly exists; deferred profile refused')
    destination.mkdir(mode=0o755)
    # Original recipe performs canonical copy/verification, artifact/help binding,
    # source identity checks and its existing cleanup. Never modify the source.
    return restore(source, app, bundle, target)
