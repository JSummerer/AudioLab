"""Test isolation: every test gets its own data and output folder and fresh preferences.

The environment is set here, before any test module imports `audiolab`, because
`settings.LIMITS` is read once at import time.
"""

import os
import tempfile
from pathlib import Path

import pytest

_SESSION_DIR = Path(tempfile.mkdtemp(prefix='audiolab_tests_'))
os.environ['AUDIOLAB_DATA_DIR'] = str(_SESSION_DIR / 'data')
os.environ['AUDIOLAB_OUTPUT_DIR'] = str(_SESSION_DIR / 'out')

from audiolab.settings import SETTINGS  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Per-test data/output folders (SETTINGS reads them from the environment on every access)."""
    monkeypatch.setenv('AUDIOLAB_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('AUDIOLAB_OUTPUT_DIR', str(tmp_path / 'out'))
    SETTINGS._settings = None
    yield tmp_path
    SETTINGS._settings = None


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """The update code (update check, downloader update) never reaches GitHub from a test: every request goes through
    `updates.open_github`, which fails the test here. Tests that need an answer patch it or `updates.fetch_json`."""
    from audiolab import updates

    def refuse(url, accept=''):
        pytest.fail(f'A test tried to contact GitHub: {url}')  # BaseException: no `except Exception` hides it

    monkeypatch.setattr(updates, 'open_github', refuse)
