"""Include the dependency-free browser regressions in the normal pytest check (Node 22+)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_web_regressions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node 22+ is needed for the web regressions')
    version = subprocess.run([node, '--version'], capture_output=True, text=True, check=True, timeout=10)
    if int(version.stdout.strip().lstrip('v').split('.')[0]) < 22:
        pytest.skip('Node 22+ is needed for production JavaScript ES modules')
    result = subprocess.run(
        [node, '--test', *map(str, sorted((ROOT / 'tests' / 'js').glob('*.test.mjs')))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
