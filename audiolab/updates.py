"""Is there a newer AudioLab or downloader? A small check against GitHub's public release lists.

Nothing is downloaded or run by the check: it names the newest AudioLab release that applies to this build
(with a link to its page on github.com/JSummerer/AudioLab) and yt-dlp's newest stable release. The result is
kept in `<data>/updates.json`; `GET /api/health` only reads it. A check runs when the user clicks "Check now"
or, with the `check_updates` setting on, at most once a day while the app is open.

The GitHub helpers here (fixed hosts, size limits, plain-language errors) are also used to fetch the packaged
app's downloader update (media/ytdlp_update.py).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .settings import SETTINGS
from .system import replace_file

log = logging.getLogger('audiolab')

REPOSITORY = 'JSummerer/AudioLab'
RELEASES_API = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=20'
RELEASE_PAGE = f'https://github.com/{REPOSITORY}/releases/tag/'
YTDLP_RELEASE_API = 'https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest'
YTDLP_DOWNLOAD = 'https://github.com/yt-dlp/yt-dlp/releases/download/'
YTDLP_ASSET = 'yt-dlp'  # the zipimport build: yt_dlp plus its matching challenge-solver package, one file
CHECK_EVERY_S = 24 * 3600  # the automatic check, also after a failed one: never more often (docs/API.md)
TIMEOUT_S = 15
_HOSTS = frozenset(
    {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
)
_APP_TAG = re.compile(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:(a|b|rc)(\d{1,4}))?\Z')
_STAGES = {'a': 0, 'b': 1, 'rc': 2, None: 3}  # 3 = final release
YTDLP_TAG = re.compile(r'\d{4}\.\d{2}\.\d{2}(?:\.\d{1,6})?\Z')
_MAX_JSON = 4 * 1024**2
_MAX_YTDLP_BYTES = 32 * 1024**2
_CACHE = 'updates.json'
_check_lock = threading.Lock()
_checking = False
_last_check: dict[Path, float] = {}  # per record file: "once a day" holds even when it can't be written
_scheduler_lock = threading.Lock()
_scheduler: threading.Thread | None = None
_on_checked: Callable[[], None] | None = None


class UpdateCheckError(Exception):
    """A plain-language reason a GitHub request failed (shown to the user as is)."""


# ----------------------------------------------------------------- versions
def app_version_key(text: Any) -> tuple[int, int, int, int, int] | None:
    """Sort key for AudioLab versions/tags: X.Y.Z, and the beta-era X.Y.ZbN / X.Y.ZrcN (and aN)."""
    m = _APP_TAG.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    major, minor, patch, stage, number = m.groups()
    return int(major), int(minor), int(patch), _STAGES[stage], int(number or 0)


def newest_app_release(releases: Any, current: str) -> str | None:
    """The newest published tag that applies to `current`: a beta build also considers betas, a final build only
    final releases. GitHub's own prerelease flag is not used, only the version itself."""
    now = app_version_key(current)
    beta_build = now is None or now[3] < 3
    best: tuple[tuple, str] | None = None
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or release.get('draft'):
            continue
        tag = release.get('tag_name')
        key = app_version_key(tag)
        if key is None or (key[3] < 3 and not beta_build):
            continue
        if best is None or key > best[0]:
            best = (key, tag)
    return best[1] if best else None


def ytdlp_version_key(text: Any) -> tuple[int, ...] | None:
    """yt-dlp versions are dates (2026.08.19, also written 2026.8.19) with an optional hotfix number."""
    if not isinstance(text, str) or not re.fullmatch(r'\d{1,4}(?:\.\d{1,6}){1,3}', text.strip()):
        return None
    return tuple(int(part) for part in text.strip().split('.'))


def ytdlp_newer(candidate: Any, than: Any) -> bool:
    new, old = ytdlp_version_key(candidate), ytdlp_version_key(than)
    return new is not None and (old is None or new > old)


# ------------------------------------------------------------------- GitHub
def check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != 'https'
        or parts.hostname not in _HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise UpdateCheckError('GitHub sent AudioLab to an unexpected address, so the check stopped.')


class _GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _plain(exc: Exception) -> UpdateCheckError:
    if isinstance(exc, UpdateCheckError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429) and (exc.headers or {}).get('X-RateLimit-Remaining') == '0':
            return UpdateCheckError('GitHub limits how often AudioLab may ask. Try again in an hour.')
        return UpdateCheckError(f'GitHub answered with an error (HTTP {exc.code}). Try again later.')
    if isinstance(exc, TimeoutError) or 'timed out' in str(exc).lower():
        return UpdateCheckError('GitHub did not answer in time. Try again later.')
    if isinstance(exc, urllib.error.URLError):
        return UpdateCheckError('No internet connection to GitHub. Connect and try again.')
    return UpdateCheckError('The connection to GitHub failed. Try again later.')


def open_github(url: str, accept: str = 'application/vnd.github+json'):
    """An HTTPS response from GitHub's fixed hosts only (redirects included), else UpdateCheckError."""
    check_url(url)
    request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'AudioLab'})
    try:
        return urllib.request.build_opener(_GitHubRedirect()).open(request, timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - every failure becomes one plain sentence
        raise _plain(exc) from exc


def read_limited(response, limit: int) -> bytes:
    try:
        data = response.read(limit + 1)
    except Exception as exc:  # noqa: BLE001
        raise _plain(exc) from exc
    if len(data) > limit:
        raise UpdateCheckError('GitHub sent more data than expected, so the check stopped.')
    return data


def fetch_json(url: str) -> Any:
    with open_github(url) as response:
        data = read_limited(response, _MAX_JSON)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise UpdateCheckError('GitHub sent an answer AudioLab could not read. Try again later.') from exc


def ytdlp_release() -> dict[str, Any]:
    """yt-dlp's newest stable release and its zipimport build: {version, url, size, sha256 | None}."""
    release = fetch_json(YTDLP_RELEASE_API)
    tag = release.get('tag_name') if isinstance(release, dict) else None
    if not isinstance(tag, str) or not YTDLP_TAG.fullmatch(tag) or release.get('draft') or release.get('prerelease'):
        raise UpdateCheckError('GitHub did not name a usable downloader release. Try again later.')
    asset = next((a for a in release.get('assets') or [] if isinstance(a, dict) and a.get('name') == YTDLP_ASSET), None)
    url = f'{YTDLP_DOWNLOAD}{tag}/{YTDLP_ASSET}'
    if asset is None or asset.get('browser_download_url') != url:
        raise UpdateCheckError('The downloader release has no file AudioLab can use. Try again later.')
    size = asset.get('size')
    if type(size) is not int or not 0 < size <= _MAX_YTDLP_BYTES:
        raise UpdateCheckError('The downloader release has an unexpected size, so AudioLab did not use it.')
    digest = asset.get('digest')
    sha256 = digest[7:].lower() if isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest) else None
    return {'version': tag, 'url': url, 'size': size, 'sha256': sha256}


# -------------------------------------------------------------------- cache
def _cache_path() -> Path:
    return SETTINGS.data_dir / _CACHE


def _load() -> dict[str, Any]:
    try:
        path = _cache_path()
        if path.is_symlink() or path.stat().st_size > 16_384:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(record: dict[str, Any]) -> None:
    path = _cache_path()
    temporary = path.with_name(f'.{path.name}-{uuid.uuid4().hex}')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
        replace_file(temporary, path)
    except OSError as exc:
        log.warning('Could not save the update check: %s', exc)
    finally:
        temporary.unlink(missing_ok=True)


def downloader_latest() -> str | None:
    value = _load().get('downloader_latest')
    return value if isinstance(value, str) and YTDLP_TAG.fullmatch(value) else None


def note_downloader_latest(version: str) -> None:
    """An update fetch also learned yt-dlp's newest release; keep it for `downloader.latest`."""
    if YTDLP_TAG.fullmatch(version or ''):
        record = _load()
        record['downloader_latest'] = version
        _save(record)


def cached() -> dict[str, Any]:
    """The `updates` object of Health, from the last check only (never the network)."""
    record = _load()
    tag = record.get('app_tag')
    tag = tag if app_version_key(tag) else None
    checked = record.get('checked_at')
    error = record.get('error')
    current, latest = app_version_key(__version__), app_version_key(tag)
    return {
        'enabled': bool(SETTINGS.get().check_updates),
        'checking': _checking,
        'checked_at': float(checked) if isinstance(checked, (int, float)) else None,
        'error': error if isinstance(error, str) else None,
        'app': {
            'latest': tag.removeprefix('v') if tag else None,
            'available': bool(latest and current and latest > current),
            'url': RELEASE_PAGE + urllib.parse.quote(tag) if tag else None,
        },
    }


# -------------------------------------------------------------------- check
def check() -> dict[str, Any]:
    """Ask GitHub now; returns `cached()`. One check at a time; a failed part keeps the last known answer."""
    global _checking
    with _check_lock:
        _checking = True
        try:
            record, errors = _load(), []
            try:
                record['app_tag'] = newest_app_release(fetch_json(RELEASES_API), __version__)
            except UpdateCheckError as exc:
                errors.append(str(exc))
            try:
                record['downloader_latest'] = ytdlp_release()['version']
            except UpdateCheckError as exc:
                errors.append(str(exc))
            record['checked_at'] = _last_check[_cache_path()] = time.time()
            record['error'] = errors[0] if errors else None
            _save(record)
        finally:
            _checking = False
    return cached()


def due(now: float | None = None) -> bool:
    """The automatic check is on and the last one, failed or not, is at least a day old (or there was none).
    The last check's time is also kept in memory, so a record that can't be saved never makes it due sooner."""
    if not SETTINGS.get().check_updates:
        return False
    saved = _load().get('checked_at')
    times = [t for t in (saved, _last_check.get(_cache_path())) if isinstance(t, (int, float))]
    if not times:
        return True
    return (time.time() if now is None else now) - max(times) >= CHECK_EVERY_S


def ensure_scheduler(on_checked: Callable[[], None] | None = None) -> None:
    """Start the one background thread that runs the daily check while `check_updates` is on."""
    global _scheduler, _on_checked
    with _scheduler_lock:
        _on_checked = on_checked
        if _scheduler is not None:
            return
        _scheduler = threading.Thread(target=_schedule, name='audiolab-update-check', daemon=True)
        _scheduler.start()


def _schedule() -> None:
    time.sleep(60)  # not during startup
    while True:
        try:
            if due():
                check()
                if _on_checked is not None:
                    _on_checked()
        except Exception:  # noqa: BLE001 - a background check must never take the app down
            log.exception('Automatic update check failed')
        time.sleep(600)
