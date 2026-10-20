"""Optional AI pack (PyTorch + Demucs).

Nothing here imports torch at module import time: the core app must start and
work without the AI pack. `ai_status()` does a cheap `find_spec` check first
and only then imports torch (slow, ~1-2 s) once; the result is cached until
`ai_status(refresh=True)` (e.g. after installing the pack from the UI, see install.py).
"""

import importlib
import importlib.util
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}

MODELS: dict[str, dict[str, Any]] = {
    'htdemucs': {
        'label': 'Standard',
        'speed': 1.0,
        'description': 'Hybrid Transformer Demucs — good separation, fastest.',
    },
    'htdemucs_ft': {
        'label': 'Fine-tuned (slower)',
        'speed': 4.0,
        'description': 'Fine-tuned Demucs bag — cleaner vocals, about 4× slower.',
    },
}
DEFAULT_MODEL = 'htdemucs'


def model_choices() -> list[dict[str, str]]:
    return [{'key': k, 'label': v['label'], 'description': v['description']} for k, v in MODELS.items()]


def _detect() -> dict[str, Any]:
    missing = [m for m in ('torch', 'demucs') if importlib.util.find_spec(m) is None]
    if missing:
        return {
            'available': False,
            'device': None,
            'torch_version': None,
            'demucs_version': None,
            'reason': 'AI pack not installed',
        }
    try:
        torch = importlib.import_module('torch')
        demucs = importlib.import_module('demucs')
    except Exception as e:  # broken install, missing native libs...
        return {
            'available': False,
            'device': None,
            'torch_version': None,
            'demucs_version': None,
            'reason': f'AI pack could not be loaded ({type(e).__name__}: {e})',
        }
    if torch.backends.mps.is_available():
        device = 'mps'
    elif torch.cuda.is_available():
        device = 'cuda'
    else:
        device = 'cpu'
    return {
        'available': True,
        'device': device,
        'torch_version': torch.__version__,
        'demucs_version': getattr(demucs, '__version__', None),
        'reason': None,
    }


def ai_status(refresh: bool = False) -> dict[str, Any]:
    """{'available', 'device', 'torch_version', 'demucs_version', 'reason'}"""
    with _LOCK:
        if refresh or 'status' not in _CACHE:
            if refresh:
                importlib.invalidate_caches()
            _CACHE['status'] = _detect()
        return dict(_CACHE['status'])


def model_cache_dir() -> Path:
    """Where torch hub keeps downloaded model weights ($TORCH_HOME, else ~/.cache/torch)."""
    base = os.environ.get('TORCH_HOME') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(Path.home(), '.cache'), 'torch'
    )
    return Path(base) / 'hub'
