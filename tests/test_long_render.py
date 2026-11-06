"""Exercise long-render paths with short generated audio and real scratch files."""

import errno
import mmap
import threading
from contextlib import ExitStack

import numpy as np
import pytest
import soundfile as sf

from audiolab.context import Progress
from audiolab.dsp import bus, scratch
from audiolab.errors import Cancelled
from audiolab.recipes import chain
from audiolab.recipes.base import ChainParams

SR = 48000


@pytest.fixture
def source(tmp_path):
    t = np.arange(8 * SR + 37) / SR
    x = np.column_stack((0.24 * np.sin(2 * np.pi * 75 * t), 0.19 * np.sin(2 * np.pi * 75 * t)))
    x += (0.12 * np.sin(2 * np.pi * 950 * t))[:, None]
    x += (0.08 * np.sin(2 * np.pi * 280 * t) * ((t > 2) & (t < 2.8)))[:, None]
    x += (0.10 * np.sin(2 * np.pi * 7200 * t) * ((t > 4) & (t < 4.6)))[:, None]
    x *= 2.5
    x[5 * SR] = (1.8, -1.6)
    x[0, 0] = np.nan
    ref = tmp_path / 'source.wav'
    sf.write(ref, x, SR, subtype='FLOAT')
    return ref


def test_disk_render_matches_ordinary_and_reuses_scratch(source, tmp_path, monkeypatch):
    params = ChainParams(
        adaptive_lowmid_db=2.0,
        denoise='light',
        bass_db=1.5,
        presence_db=0.5,
        air_db=0.5,
        sub_harmonics=0.25,
        mono_bass_hz=90.0,
        compressor='glue',
        deess_db=3.0,
        target_lufs=-8.0,
    )
    allocated = {}
    original = scratch.ScratchBuffers.array

    def observe(self, *args, **kwargs):
        buf = original(self, *args, **kwargs)
        assert isinstance(buf.base, mmap.mmap)
        allocated[buf.base] = buf.nbytes
        return buf

    monkeypatch.setattr(scratch.ScratchBuffers, 'array', observe)
    ram, mapped = tmp_path / 'ram.flac', tmp_path / 'disk.flac'
    work = tmp_path / 'work'
    ordinary = chain.render(source, ram, params, {}, work)
    assert not allocated
    # Exercise automatic selection without creating a ten-minute recording.
    monkeypatch.setattr(chain, 'DISK_BACKED_AFTER_SECONDS', 1)
    long = chain.render(source, mapped, params, {}, work)
    for key in ('input', 'steps', 'warnings', 'adaptive_lowmid', 'deess', 'compressor', 'limiter'):
        assert ordinary[key] == long[key]
    assert long['adaptive_lowmid']['engaged'] and long['deess']['engaged']
    assert long['compressor']['engaged'] and long['limiter']['engaged']
    assert long['limiter']['timeline']['bins']
    assert ram.read_bytes() == mapped.read_bytes()
    assert len(allocated) == 6
    assert sum(allocated.values()) == sf.info(source).frames * scratch.MAX_BYTES_PER_FRAME
    assert all(mapping.closed for mapping in allocated)
    assert not list(work.iterdir())
    assert not list(tmp_path.glob('.render-*'))


@pytest.mark.parametrize('gate_passed', [True, False])
def test_disk_render_handles_stem_mix_and_gate_fallback(source, tmp_path, monkeypatch, gate_passed):
    from audiolab.ai import separation

    x, _ = sf.read(source, dtype='float32')
    np.nan_to_num(x, copy=False)
    vocals, bass = tmp_path / 'vocals.wav', tmp_path / 'bass.wav'
    sf.write(vocals, x[:-31] * 0.4, SR, subtype='FLOAT')
    sf.write(bass, np.pad(x * 0.6, ((0, 27), (0, 0))), SR, subtype='FLOAT')

    def separate(*args, **kwargs):
        return {
            'model': 'fixture',
            'device': 'cpu',
            'residual_db': -70.0 if gate_passed else -10.0,
            'gate_passed': gate_passed,
            'stems': {'vocals': str(vocals), 'bass': str(bass)},
        }

    monkeypatch.setattr(separation, 'separate', separate)
    params = ChainParams(ai_stage='stem_eq', adaptive_lowmid_db=2.0, deess_db=3.0)
    a, b = tmp_path / 'ram.flac', tmp_path / 'disk.flac'
    work = tmp_path / 'work'
    ram = chain.render(source, a, params, {}, work)
    mapped = chain.render(source, b, params, {}, work, disk_backed=True)
    assert ram['ai'] == mapped['ai'] and mapped['ai']['fallback'] is not gate_passed
    assert ram['steps'] == mapped['steps']
    assert a.read_bytes() == b.read_bytes()
    assert not list(work.iterdir())


def test_disk_render_cancel_after_encode_preserves_previous_master(source, tmp_path, monkeypatch):
    work, dest = tmp_path / 'work', tmp_path / 'master.flac'
    dest.write_bytes(b'previous master')
    cancel = threading.Event()
    mappings = []
    original = chain._write_master

    def write_then_cancel(x, sr, path, check_cancel):
        mappings.append(x.base)
        original(x, sr, path, check_cancel)
        cancel.set()

    monkeypatch.setattr(chain, '_write_master', write_then_cancel)
    with pytest.raises(Cancelled):
        chain.render(source, dest, ChainParams(), {}, work, Progress.null(cancel), disk_backed=True)
    assert dest.read_bytes() == b'previous master'
    assert len(mappings) == 1 and mappings[0].closed
    assert not list(work.iterdir())
    assert not list(tmp_path.glob('.render-*'))


def test_scratch_space_write_failure_closes_partial_file(tmp_path, monkeypatch):
    actual_file = scratch.tempfile.TemporaryFile(dir=tmp_path, buffering=0)

    class NoSpace:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            actual_file.close()

        def write(self, _):
            raise OSError(errno.ENOSPC, 'fixture disk full')

    monkeypatch.setattr(scratch.tempfile, 'TemporaryFile', lambda **_: NoSpace())
    with ExitStack() as stack, pytest.raises(OSError) as error:
        scratch.ScratchBuffers(stack, tmp_path, Progress.null().check_cancel).array((32, 2))
    assert error.value.errno == errno.ENOSPC
    assert actual_file.closed
    assert not list(tmp_path.iterdir())


def test_glue_detector_and_application_preserve_context_at_block_edges(monkeypatch):
    t = np.arange(SR + 137) / SR
    x = ((0.04 + 0.7 * (np.sin(20 * t) ** 2)) * np.sin(2400 * t)).astype(np.float32)
    expected, expected_stats = bus.glue_comp(x, sr=SR)
    original_blocks = bus.blocks

    def small_blocks(n, check_cancel=None, size=3999):
        return original_blocks(n, check_cancel, size)

    monkeypatch.setattr(bus, 'BLOCK_FRAMES', 3999)
    monkeypatch.setattr(bus, 'blocks', small_blocks)
    y, stats = bus.glue_comp(x.copy(), sr=SR, in_place=True)
    assert np.max(np.abs(y - expected)) < 1e-6
    assert {k: v for k, v in stats.items() if k != 'timeline'} == {
        k: v for k, v in expected_stats.items() if k != 'timeline'
    }
    assert stats['timeline']['bins']
