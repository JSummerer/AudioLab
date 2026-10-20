"""Smoke the actual archived helper with generated audio and no external Python."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

import numpy as np
import soundfile as sf


def extract(archive: Path, bundle: Path) -> dict:
    """Extract only regular payload, directories and direct regular-file hardlinks."""

    def safe_name(value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or '..' in path.parts
            or '\\' in value
            or ':' in value
            or path.as_posix() != value.rstrip('/')
            or path.as_posix() == '.'
        ):
            raise ValueError(f'Unsafe helper archive path: {value!r}')
        return path.as_posix()

    with tarfile.open(archive) as source:
        members = source.getmembers()
        by_name = {}
        for member in members:
            name = safe_name(member.name)
            if name in by_name or not (member.isfile() or member.isdir() or member.islnk()):
                raise ValueError(f'Duplicate or unsupported helper archive member: {name}')
            by_name[name] = member
        for name, member in by_name.items():
            if any(
                parent.as_posix() in by_name and not by_name[parent.as_posix()].isdir()
                for parent in PurePosixPath(name).parents
                if parent.as_posix() != '.'
            ):
                raise ValueError(f'Helper archive member has a non-directory parent: {name}')
            if member.islnk():
                target = safe_name(member.linkname)
                if member.size != 0 or target not in by_name or not by_name[target].isfile():
                    raise ValueError(f'Helper hardlink must name a direct regular member: {name}')
        bundle.mkdir()
        for name, member in by_name.items():
            destination = bundle.joinpath(*PurePosixPath(name).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, destination.open('xb') as output:
                    shutil.copyfileobj(stream, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        links = []
        for name, member in by_name.items():
            if not member.islnk():
                continue
            destination = bundle.joinpath(*PurePosixPath(name).parts)
            target = bundle.joinpath(*PurePosixPath(member.linkname).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(target, destination, follow_symlinks=False)
            if target.stat().st_ino != destination.stat().st_ino:
                raise RuntimeError(f'Helper hardlink does not share its target inode: {name}')
            links.append({'path': name, 'target': member.linkname, 'bytes_saved': target.stat().st_size})
    return {'regular_bytes': sum(member.size for member in members if member.isfile()), 'hardlinks': links}


def signatures(bundle: Path) -> int:
    magic = {bytes.fromhex(value) for value in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca')}
    checked = 0
    for path in bundle.rglob('*'):
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:
                continue
        subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True, capture_output=True)
        checked += 1
    return checked


def run(executable: Path, arguments: list[str], folder: Path, environment: dict, name: str) -> list[dict]:
    result = subprocess.run([str(executable), *arguments], env=environment, capture_output=True, text=True, timeout=90)
    (folder / f'{name}.stdout').write_text(result.stdout)
    (folder / f'{name}.stderr').write_text(result.stderr)
    result.check_returncode()
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def inherited_lock(executable: Path, tone: Path, folder: Path, environment: dict) -> dict:
    """An orphaned frozen worker must keep its passed operation lock alive."""
    lock = folder / 'inherited.lock'
    child_log = folder / 'orphan-worker.stdout'
    metadata = folder / 'orphan-worker.pid'
    spawner = folder / 'spawn-worker.py'
    spawner.write_text(
        'import fcntl, pathlib, subprocess, sys, time\n'
        'lock = open(sys.argv[1], "a+b")\n'
        'fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
        'output = open(sys.argv[2], "w")\n'
        'child = subprocess.Popen(sys.argv[4:], pass_fds=(lock.fileno(),), stdout=output, stderr=output)\n'
        'pathlib.Path(sys.argv[3]).write_text(str(child.pid))\n'
        'time.sleep(60)\n'
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            str(spawner),
            str(lock),
            str(child_log),
            str(metadata),
            str(executable),
            '--separate',
            str(tone),
            str(folder / 'orphan-stems'),
            'htdemucs_ft',
            '1',
        ],
        env=environment,
    )
    child = None
    try:
        deadline = time.monotonic() + 15
        while not metadata.exists():
            if parent.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('The lock test parent could not spawn its frozen worker')
            time.sleep(0.05)
        child = int(metadata.read_text())
        parent.kill()
        parent.wait(timeout=5)
        with lock.open('a+b') as competitor:
            try:
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                retained = True
            else:
                raise RuntimeError('The frozen worker dropped its inherited operation lock after parent death')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(0.1)
                    continue
                break
            else:
                raise RuntimeError('The orphaned worker did not finish its bounded lock smoke')
        rows = [json.loads(line) for line in child_log.read_text().splitlines() if line.strip().startswith('{')]
        if not rows or rows[-1].get('type') != 'result':
            raise RuntimeError('The orphaned frozen worker failed separation')
        child = None
        return {'retained_after_parent_kill': retained, 'released_after_worker_exit': True}
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass


def smoke(archive: Path, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    bundle = folder / 'extracted'
    extraction = extract(archive, bundle)
    executable = bundle / 'AudioLabAI'
    environment = dict(os.environ)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        environment.pop(name, None)
    environment.update(
        PATH='/usr/bin:/bin',
        AUDIOLAB_DATA_DIR=str(folder / 'data'),
        AUDIOLAB_OUTPUT_DIR=str(folder / 'output'),
        TORCH_HOME=str(folder / 'cache/torch'),
        HF_HOME=str(folder / 'cache/huggingface'),
        XDG_CACHE_HOME=str(folder / 'cache'),
        TMPDIR=str(folder / 'tmp'),
        HF_HUB_OFFLINE='1',
        HF_HUB_DISABLE_TELEMETRY='1',
    )
    (folder / 'tmp').mkdir()
    probe = run(executable, ['--probe'], folder, environment, 'probe')[-1]
    if (probe.get('protocol'), probe.get('available'), probe.get('models')) != (1, True, ['htdemucs', 'htdemucs_ft']):
        raise RuntimeError(f'Unexpected frozen helper probe: {probe}')
    rate = 48000
    t = np.arange(rate, dtype=np.float32) / rate
    tone = folder / 'tone.wav'
    sf.write(
        tone,
        np.stack([0.1 * np.sin(2 * np.pi * 440 * t), 0.1 * np.sin(2 * np.pi * 880 * t)], axis=1),
        rate,
        subtype='FLOAT',
    )
    checks = []
    for model in probe['models']:
        output = folder / model
        rows = run(executable, ['--separate', str(tone), str(output), model, '1'], folder, environment, model)
        if rows[-1].get('type') != 'result' or rows[-1].get('protocol') != 1:
            raise RuntimeError(f'Frozen separation did not return a result: {model}')
        result = rows[-1]['result']
        if set(result['stems']) != {'drums', 'bass', 'other', 'vocals'}:
            raise RuntimeError('The frozen helper returned unexpected sources')
        for value in result['stems'].values():
            path = Path(value).resolve()
            if not path.is_relative_to(output.resolve()):
                raise RuntimeError('The frozen helper returned a stem outside its output folder')
            samples, actual_rate = sf.read(path, dtype='float32', always_2d=True)
            if samples.shape != (rate, 2) or actual_rate != rate or not np.isfinite(samples).all():
                raise RuntimeError(f'Invalid generated-audio stem: {path}')
        checks.append(
            {
                'model': model,
                'frames': rate,
                'wall_time_s': result['wall_time_s'],
                'residual_db': result['residual_db'],
                'device': result['device'],
            }
        )
    result = {
        'ok': True,
        'extraction': extraction,
        'signed_native_files': signatures(bundle),
        'probe': probe,
        'separations': checks,
        'inherited_lock': inherited_lock(executable, tone, folder, environment),
    }
    (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    smoke(args.archive.resolve(), args.output.resolve())
