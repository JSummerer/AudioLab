"""Paths, operational limits and persisted user preferences.

Precedence: environment variable > settings.json (written by the UI) > default.
All env vars use the AUDIOLAB_ prefix (see Limits below and README).

Data layout (system.default_data_dir(), override with AUDIOLAB_DATA_DIR):
    <data>/settings.json      user preferences
    <data>/jobs/<job_id>/     one folder per job (job.json, journal.jsonl, work files)
    <data>/library/           one record per finished result + kept originals (see library.py)
    <data>/bin/               tool shims (e.g. bundled ffmpeg exposed as 'ffmpeg')
    <data>/logs/              the log file (on macOS ~/Library/Logs/AudioLab, see system.log_dir)
Finished files go to the output folder (default ~/Music/AudioLab).
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_SLUG
from .system import default_data_dir, replace_file

ENV_PREFIX = APP_SLUG.upper() + '_'
log = logging.getLogger('audiolab')


def _env(name: str, default=None):
    return os.environ.get(ENV_PREFIX + name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


def default_output_dir() -> Path:
    return Path.home() / 'Music' / APP_NAME


def validate_output_location(target: Path) -> None:
    """Generated audio must never enter the removable managed-runtime tree."""
    from .errors import AudioLabError

    ai_root = (SETTINGS.data_dir / 'ai').resolve()
    resolved = target.resolve()
    if resolved.is_relative_to(ai_root) or ai_root.is_relative_to(resolved):
        raise AudioLabError(
            'Choose an output folder separate from AudioLab’s AI storage.',
            hints=['Use a folder such as Music/AudioLab, outside the app data folder.'],
        )


# ---------------------------------------------------------------------------
# Operational limits (env only — these protect the machine, not preferences)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Limits:
    # Refuse anything longer than this before downloading (YouTube "24 h mixes").
    max_duration_s: float = _env_float('MAX_DURATION_S', 8 * 3600)
    # AI separation on CPU is ~25-40x slower than realtime; refuse long CPU runs.
    ai_cpu_max_duration_s: float = _env_float('AI_CPU_MAX_DURATION_S', 20 * 60)
    allow_unsafe_ai: bool = _env_bool('ALLOW_UNSAFE_AI', False)
    max_concurrent_jobs: int = max(1, int(_env_float('MAX_CONCURRENT_JOBS', 1)))
    max_upload_mb: float = _env_float('MAX_UPLOAD_MB', 4096)
    max_download_mb: float = _env_float('MAX_DOWNLOAD_MB', 4096)
    max_playlist_items: int = int(_env_float('MAX_PLAYLIST_ITEMS', 200))
    min_free_disk_gb: float = _env_float('MIN_FREE_DISK_GB', 2.0)
    # Fraction of detected memory the peak estimate must stay under.
    memory_safety_fraction: float = min(1.0, max(0.1, _env_float('MEMORY_SAFETY_FRACTION', 0.85)))
    # Delete finished job work folders older than N days at startup (0 = never).
    retention_days: float = _env_float('RETENTION_DAYS', 30)
    ffmpeg_timeout_s: float = _env_float('FFMPEG_TIMEOUT_S', 3 * 3600)
    ytdlp_probe_timeout_s: float = _env_float('YTDLP_PROBE_TIMEOUT_S', 90)
    ytdlp_download_timeout_s: float = _env_float('YTDLP_DOWNLOAD_TIMEOUT_S', 3 * 3600)


LIMITS = Limits()


# ---------------------------------------------------------------------------
# User preferences (persisted, editable from the UI)
# ---------------------------------------------------------------------------
@dataclass
class UserSettings:
    output_dir: str = ''  # '' = default_output_dir()
    download_format: str = 'mp3'  # see media.export.FORMATS
    enhance_formats: tuple = ('mp3_320', 'wav16')
    separate_format: str = 'wav16'
    ui_mode: str = 'simple'  # simple | advanced
    theme: str = 'auto'  # auto | light | dark
    keep_work_files: bool = False  # keep big intermediates (reference, stems) after success
    embed_cover: bool = True
    keep_originals: bool = True  # Library: keep the original of Enhance jobs for comparing
    auto_update_downloader: bool = True  # on a 'site changed' error: update yt-dlp once and retry
    cookies_browser: str = ''  # '' | firefox | chrome | edge | brave | safari | chromium | opera | vivaldi


# Settings with a fixed set of values (anything else in an update is refused).
_CHOICES = {
    'ui_mode': {'simple', 'advanced'},
    'theme': {'auto', 'light', 'dark'},
    'cookies_browser': {'', 'firefox', 'chrome', 'edge', 'brave', 'safari', 'chromium', 'opera', 'vivaldi'},
}
# Format settings: their keys come from media.export (the one source of truth), per kind.
_FORMAT_KINDS = {'download_format': 'download', 'enhance_formats': 'enhance', 'separate_format': 'separate'}
_BOOL_TEXT = {'true': True, 'false': False, '1': True, '0': False}

# Env vars that pin a preference (shown as locked in the UI).
_SETTING_ENV = {'output_dir': 'OUTPUT_DIR'}


class _Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._settings: UserSettings | None = None

    # -- paths ---------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        p = Path(_env('DATA_DIR') or default_data_dir()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def jobs_dir(self) -> Path:
        p = self.data_dir / 'jobs'
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def library_dir(self) -> Path:
        p = self.data_dir / 'library'
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def bin_dir(self) -> Path:
        p = self.data_dir / 'bin'
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def settings_file(self) -> Path:
        return self.data_dir / 'settings.json'

    @property
    def output_dir(self) -> Path:
        """The output folder as set. Not created here — it may be on a drive that isn't connected; whatever
        writes into it creates it (tasks/common.output_root, output_folder)."""
        raw = _env('OUTPUT_DIR') or self.get().output_dir or str(default_output_dir())
        return Path(raw).expanduser()

    # -- preferences ---------------------------------------------------------
    def get(self) -> UserSettings:
        with self._lock:
            if self._settings is None:
                self._settings = self._load()
            return self._settings

    def _load(self) -> UserSettings:
        s = UserSettings()
        try:
            raw = json.loads(self.settings_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return s
        if not isinstance(raw, dict):
            log.warning('Settings file is not an object; using defaults.')
            return s
        known = {f.name for f in fields(UserSettings)}
        for k, v in raw.items():
            if k in known:
                try:
                    value = _checked(k, v)
                    if k == 'output_dir' and value and not Path(value).expanduser().is_absolute():
                        raise ValueError('Output folder is not absolute.')
                except (ValueError, RuntimeError):  # expanduser can fail for an unknown ~user
                    log.warning('Ignoring invalid saved setting: %s', k)
                    continue
                setattr(s, k, value)
        return s

    def update(self, patch: dict[str, Any]) -> UserSettings:
        """Apply a partial update from the UI and persist. Unknown keys are ignored; an invalid value
        raises ValueError with a readable message, and then nothing is changed."""
        known = {f.name for f in fields(UserSettings)}
        values = {k: _checked(k, v) for k, v in (patch or {}).items() if k in known}
        self.get()
        with self._lock:
            # Publish a new snapshot only once persistence succeeds. A failed save must not
            # change active behavior, and concurrent patches must start from the latest snapshot.
            current = replace(self._settings, **values)
            tmp = self.settings_file.with_suffix('.tmp')
            try:
                tmp.write_text(json.dumps(asdict(current), indent=2), encoding='utf-8')
                replace_file(tmp, self.settings_file)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._settings = current
        return current

    def locked(self) -> list[str]:
        """The keys an environment variable pins (shown read-only, ignored in updates)."""
        return [k for k, env in _SETTING_ENV.items() if _env(env)]

    def public(self) -> dict[str, Any]:
        """Settings as the UI sees them (resolved output dir + which keys env pins)."""
        d = asdict(self.get())
        d['enhance_formats'] = list(d['enhance_formats'])
        d['output_dir'] = str(self.output_dir)
        d['locked'] = self.locked()
        return d


def _checked(key: str, v: Any) -> Any:
    """A value from the UI for the setting `key`, in its stored type; ValueError when it isn't valid."""
    default = getattr(UserSettings(), key)
    if isinstance(default, bool):
        if isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in _BOOL_TEXT:
            return _BOOL_TEXT[v.strip().lower()]
        raise ValueError(f'{key} must be true or false.')
    if isinstance(default, tuple):
        if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
            raise ValueError(f'{key} must be a list.')
        v = tuple(dict.fromkeys(x.strip() for x in v))
        if not v:
            raise ValueError(f'{key} needs at least one entry.')
    elif isinstance(default, str):
        if v is not None and not isinstance(v, str):
            raise ValueError(f'{key} must be text.')
        v = (v or '').strip()
    choices = _CHOICES.get(key)
    if key in _FORMAT_KINDS:
        from .media.export import format_choices  # not at the top: export imports this module (via ffmpeg)

        choices = {c['key'] for c in format_choices(_FORMAT_KINDS[key])}
    for x in v if isinstance(v, tuple) else (v,):
        if choices is not None and x not in choices:
            raise ValueError(f'"{x}" is not a valid choice for {key}.')
    return v


SETTINGS = _Store()
