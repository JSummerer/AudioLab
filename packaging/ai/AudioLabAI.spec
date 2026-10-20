"""Build a directory helper; release.py emits only regular archive members."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

WORK = Path(os.environ['AUDIOLAB_AI_WORK_DIR']).resolve()

a = Analysis(
    [str(WORK / 'source-snapshot/entry.py')],
    pathex=[str(WORK / 'source-snapshot')],
    binaries=[],
    datas=copy_metadata('demucs'),
    hiddenimports=['demucs.htdemucs', 'demucs.hf'],
    excludes=[
        'huggingface_hub', 'lameenc', 'sphn', 'torchaudio', 'diffq',
        'tkinter', 'pytest', 'IPython', 'matplotlib', 'webview', 'yt_dlp',
        'imageio_ffmpeg', 'deno', 'fastapi', 'uvicorn',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='AudioLabAI', console=True,
    argv_emulation=False, target_arch='arm64', codesign_identity=None, entitlements_file=None,
)
collection = COLLECT(exe, a.binaries, a.datas, name='AudioLabAI')
