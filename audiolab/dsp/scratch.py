"""Private, reusable float32 scratch arrays for one long mastering render."""

import math
import mmap
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import numpy as np

DISK_BACKED_AFTER_SECONDS = 600
# Up to three stereo work buffers and three mono limiter buffers at float32.
MAX_BYTES_PER_FRAME = 36
BLOCK_FRAMES = 1 << 20


def blocks(n: int, check_cancel=None, size: int = BLOCK_FRAMES) -> Iterator[slice]:
    for start in range(0, n, size):
        if check_cancel is not None:
            check_cancel()
        yield slice(start, min(start + size, n))


class ScratchBuffers:
    """Reuse buffers of the same shape. Callers protect any live inputs via `keep`.

    Arrays and their views stay inside the render's ExitStack, which closes each
    mapping before the underlying temporary file (also required on Windows).
    """

    def __init__(self, stack: ExitStack, folder: Path, check_cancel):
        self._stack = stack
        self._folder = folder
        self._check_cancel = check_cancel
        self._arrays: list[np.ndarray] = []

    def array(self, shape: tuple[int, ...], *, keep: tuple[np.ndarray, ...] = ()) -> np.ndarray:
        for array in self._arrays:
            if array.shape == shape and all(array is not live for live in keep):
                return array

        dtype = np.dtype(np.float32)
        remaining = math.prod(shape) * dtype.itemsize
        # Writing the file first avoids a sparse mapping that could fail with a
        # bus error on first touch when the scratch drive runs out of space.
        zero = memoryview(bytes(min(remaining, 8 << 20)))
        with ExitStack() as pending:
            file = pending.enter_context(tempfile.TemporaryFile(dir=self._folder, buffering=0))
            while remaining:
                self._check_cancel()
                count = file.write(zero[:remaining])
                if not count:
                    raise OSError('Could not allocate audio scratch space.')
                remaining -= count
            mapping = pending.enter_context(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_WRITE))
            array = np.ndarray(shape, dtype=dtype, buffer=mapping)
            self._stack.enter_context(pending.pop_all())
        self._arrays.append(array)
        return array
