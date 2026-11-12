"""Before/after excerpts for comparing when the original isn't kept: original and enhanced.

Both are written at their true relative level — the player matches the volume itself, from the
whole-song loudness ("Match volume"), so the excerpts are never matched on their own. One shared
gain keeps both below -1 dBFS while preserving their relative level.

The excerpt is a representative loud passage from the middle of the programme, found by probing
a handful of short windows (cheap even for multi-hour sets). It starts on a 1/25 s step, so its
start in the song is exact.
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

_HEADROOM = 10.0 ** (-1.0 / 20.0)  # -1 dBFS


def _pick_start(master: Path, total: int, sr: int, n: int, check_cancel=None) -> int:
    """Start of the loudest of ~16 candidate windows between 20 % and 80 %."""
    if total <= n:
        return 0
    lo, hi = int(0.2 * total), max(int(0.2 * total), int(0.8 * total) - n)
    if hi <= lo:
        return max(0, (total - n) // 2)
    probe = min(n, 5 * sr)
    best, best_e = (total - n) // 2, -1.0
    for start in np.linspace(lo, hi, 16).astype(int):
        if check_cancel:
            check_cancel()
        mid = int(start) + n // 2 - probe // 2
        blk, _ = sf.read(str(master), start=mid, stop=mid + probe, dtype='float32', always_2d=True)
        e = float((blk**2).mean()) if blk.size else 0.0
        if e > best_e:
            best, best_e = int(start), e
    return best


def _read(path: Path, start: int, n: int) -> np.ndarray:
    x, _ = sf.read(str(path), start=start, stop=start + n, dtype='float32', always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return np.nan_to_num(x[:, :2], nan=0.0, posinf=0.0, neginf=0.0)


def make_previews(
    source_ref: Path,
    master: Path,
    out_dir: Path,
    duration_s: float = 45.0,
    check_cancel=None,
    *,
    start_frame=None,
    saved_master: Path | None = None,
) -> dict[str, Any]:
    """Write aligned comparison WAVs with one common headroom gain."""
    out_dir = Path(out_dir)
    if check_cancel:
        check_cancel()
    out_dir.mkdir(parents=True, exist_ok=True)
    info_src, info_mst = sf.info(str(source_ref)), sf.info(str(master))
    sr = info_mst.samplerate
    total = min(info_src.frames, info_mst.frames)
    refs = {'original': source_ref, 'enhanced': master}
    if saved_master is not None:
        total = min(total, sf.info(saved_master).frames)
        refs['saved'] = saved_master
    if start_frame is None:
        n = min(total, int(duration_s * sr))
        start = _pick_start(Path(master), total, sr, n, check_cancel)
        start -= start % max(1, sr // 25)
    else:
        start = start_frame
        n = min(total - start, round(duration_s * sr))

    crops = {key: _read(path, start, n) for key, path in refs.items()}
    m = min(map(len, crops.values()))
    peak = max(float(np.abs(data[:m]).max(initial=0.0)) for data in crops.values())
    shared = min(1.0, _HEADROOM / peak) if peak > 0 else 1.0
    paths = {}
    subtype = 'PCM_24' if saved_master is not None else 'PCM_16'
    for key, data in crops.items():
        if check_cancel:
            check_cancel()
        paths[key] = str(out_dir / f'preview_{key}.wav')
        sf.write(paths[key], np.clip(data[:m] * np.float32(shared), -1.0, 1.0), sr, subtype=subtype, format='WAV')

    return {
        **paths,
        'start_s': round(start / sr, 3),
        'duration_s': round(m / sr, 3),
        'frames': m,
        'shared_gain_db': 20.0 * math.log10(shared),
    }
