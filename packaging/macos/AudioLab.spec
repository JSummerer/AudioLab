# Build with a clean base-only Python environment; this is a directory bundle, not a self-extractor.
import os
import runpy
from pathlib import Path

import deno
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
FFMPEG = Path(os.environ['AUDIOLAB_PACKAGE_FFMPEG']).resolve()
assert FFMPEG.is_file(), 'Build the pinned media tool before packaging'
NOTICES = Path(os.environ['AUDIOLAB_PACKAGE_NOTICES']).resolve()
assert NOTICES.is_dir() and (NOTICES / 'packages.json').is_file(), 'Prepare the exact shipped third-party notices'
assert (NOTICES / 'build-info.json').is_file(), 'Prepare the build inventory before packaging'
DENO = Path(deno.find_deno_bin()).resolve()
INVENTORY = runpy.run_path(str(ROOT / 'packaging/macos/bundle_inventory.py'))
INVENTORY['validate_deno'](NOTICES, DENO)
VERSION = runpy.run_path(str(ROOT / 'audiolab/__init__.py'))['__version__']
SHORT_VERSION = VERSION.split('b', 1)[0]

datas = [
    (str(ROOT / 'audiolab/web'), 'audiolab/web'),
    (str(ROOT / 'audiolab/assets'), 'audiolab/assets'),
    (str(ROOT / 'LICENSE'), '.'),
    (str(NOTICES), 'ThirdParty'),
    (str(NOTICES / 'build-info.json'), '.'),
]
for package in ('yt_dlp', 'yt_dlp_ejs', 'certifi', 'webview'):
    datas += collect_data_files(package)
for distribution in ('yt-dlp', 'yt-dlp-ejs', 'pywebview'):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(ROOT / 'packaging/macos/entry.py')],
    pathex=[str(ROOT)],
    binaries=[(str(FFMPEG), 'tools'), (str(DENO), 'tools')],
    datas=datas,
    hiddenimports=(
        collect_submodules('audiolab')
        + collect_submodules('yt_dlp', filter=lambda name: not name.startswith('yt_dlp.__pyinstaller'))
        + ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
           'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'webview.platforms.cocoa']
    ),
    excludes=['torch', 'torchaudio', 'demucs', 'mutagen', 'imageio_ffmpeg',
              'pytest', '_pytest', 'pluggy', 'iniconfig', 'pygments', 'ruff',
              'PyInstaller', 'altgraph', 'macholib', 'httpx', 'httpcore',
              'tkinter', 'PyQt5', 'PyQt6', 'PySide6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='AudioLab',
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(exe, a.binaries, a.datas, name='AudioLab')
app = BUNDLE(
    collection,
    name='AudioLab.app',
    icon=str(ROOT / 'audiolab/assets/AppIcon.icns'),
    bundle_identifier='io.github.jsummerer.audiolab',
    version=VERSION,
    info_plist={
        'CFBundleShortVersionString': SHORT_VERSION,
        'CFBundleVersion': VERSION,
        'LSMinimumSystemVersion': INVENTORY['MINIMUM_MACOS'],
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    },
)
