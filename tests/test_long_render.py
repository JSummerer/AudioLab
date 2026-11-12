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


@pytest.fixture
def section_eq():
    bands = [{'hz': 950.0, 'db': -3.0, 'q': 1.0}]
    return [
        {'start_s': 1.0, 'end_s': 3.0, 'fade_s': 0.5, 'bands': bands},
        {'start_s': 4.0, 'end_s': 7.0, 'fade_s': 0.5, 'bands': bands},
    ]


def test_disk_render_matches_ordinary_and_reuses_scratch(source, section_eq, tmp_path, monkeypatch):
    params = ChainParams(
        adaptive_lowmid_db=2.0,
        denoise='light',
        bass_db=1.5,
        presence_db=0.5,
        air_db=0.5,
        section_eq=section_eq,
        sub_harmonics=0.25,
        mono_bass_hz=90.0,
        compressor='glue',
        deess_db=3.0,
        target_lufs=-8.0,
    )
    allocated = {}
    section_mappings = []
    section_sos = chain.peak_fq(950.0, -3.0, q=1.0, sr=SR)
    original_array = scratch.ScratchBuffers.array
    original_filter = chain.filtfilt_stream

    def observe(self, *args, **kwargs):
        buf = original_array(self, *args, **kwargs)
        assert isinstance(buf.base, mmap.mmap)
        allocated[buf.base] = buf.nbytes
        return buf

    def observe_section(sos, x, *, check_cancel, out):
        if np.array_equal(sos, section_sos):
            if out is not None:
                assert out.shape == x.shape and out.dtype == np.float32
                assert not np.shares_memory(out, x)
            section_mappings.append(out.base.base if out is not None else None)
        return original_filter(sos, x, check_cancel=check_cancel, out=out)

    monkeypatch.setattr(scratch.ScratchBuffers, 'array', observe)
    monkeypatch.setattr(chain, 'filtfilt_stream', observe_section)
    ram, mapped = tmp_path / 'ram.flac', tmp_path / 'disk.flac'
    work = tmp_path / 'work'
    ordinary = chain.render(source, ram, params, {}, work)
    assert not allocated
    assert section_mappings == [None, None]
    # Exercise automatic selection without creating a ten-minute recording.
    monkeypatch.setattr(chain, 'DISK_BACKED_AFTER_SECONDS', 1)
    long = chain.render(source, mapped, params, {}, work)
    for key in ('input', 'steps', 'warnings', 'adaptive_lowmid', 'deess', 'compressor', 'limiter'):
        assert ordinary[key] == long[key]
    assert long['adaptive_lowmid']['engaged'] and long['deess']['engaged']
    assert long['compressor']['engaged'] and long['limiter']['engaged']
    assert long['limiter']['timeline']['bins']
    assert 'Applied your section EQ to 2 parts of the recording' in long['steps']
    assert ram.read_bytes() == mapped.read_bytes()
    assert len(section_mappings) == 4
    assert section_mappings[2] in allocated and section_mappings[2] is section_mappings[3]
    assert len(allocated) == 6
    assert sum(allocated.values()) == sf.info(source).frames * scratch.MAX_BYTES_PER_FRAME
    assert all(mapping.closed for mapping in allocated)
    assert not list(work.iterdir())
    assert not list(tmp_path.glob('.render-*'))


@pytest.mark.parametrize('gate_passed', [True, False])
def test_disk_render_handles_stem_mix_and_gate_fallback(source, section_eq, tmp_path, monkeypatch, gate_passed):
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
    params = ChainParams(ai_stage='stem_eq', adaptive_lowmid_db=2.0, section_eq=section_eq, deess_db=3.0)
    a, b = tmp_path / 'ram.flac', tmp_path / 'disk.flac'
    work = tmp_path / 'work'
    ram = chain.render(source, a, params, {}, work)
    mapped = chain.render(source, b, params, {}, work, disk_backed=True)
    assert ram['ai'] == mapped['ai'] and mapped['ai']['fallback'] is not gate_passed
    assert ram['steps'] == mapped['steps']
    assert ram['adaptive_lowmid'] == mapped['adaptive_lowmid']
    if gate_passed:
        assert mapped['adaptive_lowmid'] == {'engaged': False, 'skip_reason': 'AI stem EQ already shapes the low-mids'}
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


def test_disk_render_cancel_in_section_eq_closes_filter_buffer(source, section_eq, tmp_path, monkeypatch):
    work, dest = tmp_path / 'work', tmp_path / 'master.flac'
    dest.write_bytes(b'previous master')
    cancel = threading.Event()
    mappings = []
    original_filter = chain.filtfilt_stream

    def cancel_before_filtering(sos, x, *, check_cancel, out):
        mappings.append(out.base.base)
        cancel.set()
        return original_filter(sos, x, check_cancel=check_cancel, out=out)

    monkeypatch.setattr(chain, 'filtfilt_stream', cancel_before_filtering)
    params = ChainParams(highpass_hz=0, section_eq=section_eq)
    with pytest.raises(Cancelled):
        chain.render(source, dest, params, {}, work, Progress.null(cancel), disk_backed=True)
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
