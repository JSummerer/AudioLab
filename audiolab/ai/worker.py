"""Private entry point for the optional frozen AI runtime. No server or installer."""

import argparse
import json
import os
import sys
from pathlib import Path

from .. import runtime
from ..context import Progress
from . import MODELS

PROTOCOL = 1


def _emit(kind: str, **values) -> None:
    print(json.dumps({'type': kind, **values}, allow_nan=False), flush=True)


def models_dir() -> Path:
    return Path(os.environ.get('AUDIOLAB_AI_MODELS_DIR') or runtime.ai_worker_root() / 'models')


def model_definition(name: str) -> dict:
    import yaml

    if name not in MODELS:
        raise ValueError('Unknown AI model.')
    root = models_dir()
    definition = yaml.safe_load((root / f'{name}.yaml').read_text())
    signatures = definition['models']
    if not isinstance(signatures, list) or not signatures:
        raise ValueError('The installed model definition is incomplete.')
    for signature in signatures:
        if not isinstance(signature, str) or len(signature) != 8 or any(c not in '0123456789abcdef' for c in signature):
            raise ValueError('The installed model definition is invalid.')
        if not (root / f'{signature}.safetensors').is_file():
            raise ValueError('An installed AI model file is missing. Repair the AI add-on in Settings.')
    return definition


def load_local_model(name: str):
    """Use only the pinned installed weights, never remote resolution or a shared cache."""
    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model

    definition = model_definition(name)
    models = [load_safetensors_model(models_dir() / f'{signature}.safetensors') for signature in definition['models']]
    return BagOfModels(models, definition.get('weights'), definition.get('segment')).eval()


def probe() -> dict:
    from . import ai_status

    pack = json.loads((runtime.ai_worker_root() / 'pack.json').read_text())
    if pack['protocol'] != PROTOCOL or pack['models'] != list(MODELS):
        raise ValueError('The installed AI runtime is incompatible.')
    for name in MODELS:
        model_definition(name)
    status = ai_status(refresh=True)
    if not status['available']:
        raise RuntimeError(status['reason'])
    return {**status, 'protocol': PROTOCOL, 'version': pack['version'], 'models': list(MODELS)}


def main(argv=None) -> int:
    # Set internally, not through inherited user environment: the main app cannot
    # accidentally import Torch or bypass the managed runtime boundary.
    sys._audiolab_ai_worker = True
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    parser = argparse.ArgumentParser(description='AudioLab private AI helper')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--probe', action='store_true')
    modes.add_argument('--separate', nargs=4, metavar=('INPUT', 'OUTPUT', 'MODEL', 'SHIFTS'))
    args = parser.parse_args(argv)
    try:
        if args.probe:
            _emit('probe', **probe())
        else:
            from .separation import separate

            source, output, model, shifts = args.separate
            if model not in MODELS or not 1 <= int(shifts) <= 10:
                raise ValueError('Invalid separation options.')
            progress = Progress.null()
            progress._shared.emit = lambda pct, message: _emit('progress', pct=pct, message=message)
            result = separate(Path(source), Path(output), model=model, shifts=int(shifts), progress=progress)
            _emit('result', protocol=PROTOCOL, result=result)
    except Exception as exc:
        _emit('error', message=str(exc)[:1000])
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
