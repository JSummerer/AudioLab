"""Small reduction summaries for comparison, without retaining full-rate controls.

Each bin reports its strongest observed reduction and that observation's time.
It is a highlight of the interval, not a claim that the gain stayed constant.
"""

import math

import numpy as np

MAX_BINS = 120
MIN_REDUCTION_DB = 0.1


class ReductionTimeline:
    """Collect sequential or blockwise reduction measurements on the source frame grid."""

    def __init__(self, frames: int, sr: int):
        self.frames = max(0, int(frames))
        self.sr = sr
        self.width = max(1, math.ceil(self.frames / MAX_BINS))
        self.peaks: dict[int, tuple[float, float]] = {}

    def add(self, reductions_db: np.ndarray, start_frame: float = 0, step_frames: int = 1) -> None:
        """Observe positive reduction in dB. A control-rate caller supplies its step and centre."""
        if len(reductions_db) == 0 or self.frames == 0:
            return
        if step_frames <= 0:
            raise ValueError('Timeline step must be positive.')
        first = max(0, math.floor(start_frame / self.width))
        last = min(
            (self.frames - 1) // self.width,
            math.floor((start_frame + (len(reductions_db) - 1) * step_frames) / self.width),
        )
        for index in range(first, last + 1):
            lo = max(0, math.ceil((index * self.width - start_frame) / step_frames))
            hi = min(
                len(reductions_db), math.ceil((min(self.frames, (index + 1) * self.width) - start_frame) / step_frames)
            )
            if hi <= lo:
                continue
            values = reductions_db[lo:hi]
            # Only a block/bin-sized temporary; invalid measurements never become UI claims.
            finite = np.where(np.isfinite(values), values, -np.inf)
            at = int(np.argmax(finite))
            peak = float(finite[at])
            if peak < MIN_REDUCTION_DB or peak <= self.peaks.get(index, (0.0, 0.0))[0]:
                continue
            self.peaks[index] = (peak, start_frame + (lo + at) * step_frames)

    def result(self) -> dict:
        return {
            'duration_s': round(self.frames / self.sr, 6),
            'bins': [
                {
                    'start_s': round(index * self.width / self.sr, 6),
                    'end_s': round(min(self.frames, (index + 1) * self.width) / self.sr, 6),
                    'peak_s': round(frame / self.sr, 6),
                    'max_gr_db': round(peak, 3),
                }
                for index, (peak, frame) in sorted(self.peaks.items())
            ],
        }


def reduction_timeline(
    reductions_db: np.ndarray,
    frames: int,
    sr: int,
    *,
    step_frames: int = 1,
    start_frame: float = 0,
) -> dict:
    """Summarize a control-rate array; full-rate callers should use blockwise `add`."""
    timeline = ReductionTimeline(frames, sr)
    timeline.add(reductions_db, start_frame, step_frames)
    return timeline.result()
