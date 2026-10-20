"""The small differences between a source install and the self-contained app.

The frozen executable is an app launcher, not a general Python interpreter. Its
private downloader mode and bundled tools are part of the packaging contract.
"""

import os
import sys
import tempfile
from pathlib import Path


def is_packaged() -> bool:
    return bool(getattr(sys, 'frozen', False))


def is_ai_worker() -> bool:
    """The optional frozen helper uses local Torch, never another helper."""
    return bool(getattr(sys, '_audiolab_ai_worker', False))


def ai_worker_root() -> Path:
    return Path(sys.executable).resolve().parent


def downloader_command() -> list[str]:
    if is_packaged():
        return [sys.executable, '--audiolab-downloader']
    return [sys.executable, '-m', 'yt_dlp']


def bundled_tool(name: str) -> Path | None:
    """An executable shipped by our packager, never one from the user's PATH."""
    if name not in ('ffmpeg', 'deno'):
        raise ValueError(f'Unknown bundled tool: {name}')
    bundle = getattr(sys, '_MEIPASS', None)
    if not is_packaged() or bundle is None:
        return None
    path = Path(bundle) / 'tools' / name
    return path if path.is_file() and os.access(path, os.X_OK) else None


def configure_packaged() -> None:
    """Set trust and writable cache/temp locations before the app or a worker starts.

    Source installs retain their existing environment. Nothing here creates an
    output folder or writes into the app bundle.
    """
    if not is_packaged():
        return
    import certifi

    from .settings import SETTINGS

    certificates = Path(certifi.where())
    if not certificates.is_file():
        raise RuntimeError('The app is missing its security certificates. Replace it with a fresh copy of AudioLab.')
    cache = SETTINGS.data_dir / 'cache'
    deno_cache, temp = cache / 'deno', cache / 'tmp'
    deno_cache.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    os.environ['SSL_CERT_FILE'] = str(certificates)
    os.environ['DENO_DIR'] = str(deno_cache)
    os.environ['TMPDIR'] = str(temp)
    tempfile.tempdir = str(temp)  # tempfile may already have cached the system temp folder
