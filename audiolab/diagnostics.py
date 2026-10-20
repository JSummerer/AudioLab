"""The log file and the plain-text system report for bug reports
(`GET /api/diagnostics`, `audiolab doctor --report`).

The report never includes the access token. The home folder is shown as `~` so
reports can be pasted publicly without leaking the user name.
"""

import json
import logging
import os
import platform
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_SLUG, __version__, system
from .resources import disk_free_bytes, memory_report
from .settings import LIMITS, SETTINGS
from .util import human_bytes

LOG_LINES = 80
JOURNAL_EVENTS = 200


def log_file() -> Path:
    """The app's log file (see system.log_dir for where it lives)."""
    return system.log_dir(SETTINGS.data_dir) / f'{APP_SLUG}.log'


class _LogFileHandler(RotatingFileHandler):
    """Marks the handler setup_logging() installed."""


def setup_logging() -> Path:
    """Send the app logger to log_file() (rotating, ~3 MB total). One file per process:
    a handler for another data folder's log is replaced."""
    path = log_file()
    logger = logging.getLogger('audiolab')
    for h in [h for h in logger.handlers if isinstance(h, _LogFileHandler)]:
        if h.baseFilename == os.path.abspath(path):
            return path
        logger.removeHandler(h)
        h.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _LogFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    except OSError:
        return path
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return path


_URL = re.compile(r'(https?://)([^\s/@]+@)?([^\s?#"\']+)(\?[^\s#"\']*)?', re.I)
_SECRET_PARAM = re.compile(
    r'(token|key|sig|signature|auth|pass|password|secret|session|cred|credential|'
    r'policy|expires|x-amz-[a-z-]+)$',
    re.I,
)


def _redact_url(m: 're.Match') -> str:
    scheme, rest, query = m.group(1), m.group(3), m.group(4) or ''  # group 2 = user:password@
    if query:
        parts = []
        for pair in query[1:].split('&'):
            name, eq, _value = pair.partition('=')
            parts.append(f'{name}=***' if eq and _SECRET_PARAM.search(name) else pair)
        query = '?' + '&'.join(parts)
    return scheme + rest + query  # user:password@ is dropped


def _private(text: str) -> str:
    """Home folder → ~, credentials inside links (user:pass@, ?token=…, signed URLs) → removed."""
    home = str(Path.home())
    if home and home != '/':
        text = text.replace(home, '~')
    return _URL.sub(_redact_url, text)


def _tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()[-200_000:].splitlines()[-n:]
    except OSError:
        return []


def _fmt_ts(ts: Any) -> str:
    if isinstance(ts, (int, float)) and ts:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
    return str(ts).replace('T', ' ') if ts else '-'


def build_report(
    health: dict[str, Any],
    jobs: Iterable[dict[str, Any]] = (),
    job: dict[str, Any] | None = None,
    journal: Iterable[dict[str, Any]] = (),
    log_path: Path | None = None,
) -> str:
    """`health` is the /api/health payload; `job`/`journal` optionally one job's details."""
    out: list[str] = []
    add = out.append
    add(f'{APP_NAME} {__version__} — diagnostics, {time.strftime("%Y-%m-%d %H:%M:%S")}')
    add('')
    mem = memory_report()
    add(
        f'System:      {platform.platform()} · {platform.machine()} · Python {platform.python_version()} '
        f'({sys.executable})'
    )
    add(
        f'Memory:      {human_bytes(mem.get("limit_bytes") or 0)} total, '
        f'{human_bytes(mem.get("available_bytes") or 0)} usable ({mem.get("source", "?")}) · CPUs: {os.cpu_count()}'
    )
    add(f'Mode:        {"this computer" if health.get("local") else "server / Docker"}')
    ff = health.get('ffmpeg') or {}
    add(
        f'FFmpeg:      {"ok" if ff.get("ok") else "MISSING"} {ff.get("version") or ""} ({ff.get("source") or "-"})'
        + (f' — {ff["error"]}' if ff.get('error') else '')
    )
    dl = health.get('downloader') or {}
    age = dl.get('age_days')
    add(f'Downloader:  yt-dlp {dl.get("version") or "MISSING"}' + (f' ({age} days old)' if age is not None else ''))
    ai = {k: v for k, v in (health.get('ai') or {}).items() if k != 'install_log'}
    add(f'AI:          {json.dumps(ai, default=str)}')
    out_dir, data_dir = SETTINGS.output_dir, SETTINGS.data_dir
    add(f'Output:      {out_dir} (free {human_bytes(disk_free_bytes(out_dir) or 0)})')
    add(f'Data:        {data_dir} (free {human_bytes(disk_free_bytes(data_dir) or 0)})')
    st = SETTINGS.public()
    add('Settings:    ' + ', '.join(f'{k}={v}' for k, v in st.items() if k not in ('output_dir',)))
    add('Limits:      ' + ', '.join(f'{k}={v}' for k, v in vars(LIMITS).items()))
    jobs = list(jobs)
    counts = Counter(j.get('status') for j in jobs)
    add(f'Jobs:        {len(jobs)} ({", ".join(f"{n} {s}" for s, n in counts.most_common()) or "none"})')

    if job:
        add('')
        add(f'--- job {job.get("id")} ---')
        src = job.get('source') or {}
        add(
            f'Task:        {job.get("task")} · status {job.get("status")} · stage {job.get("stage") or "-"} '
            f'· {job.get("progress")} %'
        )
        add(
            f'Source:      {src.get("kind")} {src.get("url") or src.get("name") or ""}'
            + (f' · {src["duration_s"]:.0f} s' if isinstance(src.get('duration_s'), (int, float)) else '')
        )
        add(f'Options:     {json.dumps(job.get("options") or {}, default=str)}')
        add(
            f'Times:       created {_fmt_ts(job.get("created_at"))}, started {_fmt_ts(job.get("started_at"))}, '
            f'finished {_fmt_ts(job.get("finished_at"))}'
        )
        if job.get('error'):
            add(f'Error:       {json.dumps(job["error"], default=str)}')
        add('Journal:')
        for ev in list(journal)[-JOURNAL_EVENTS:]:
            line = f'  {_fmt_ts(ev.get("ts"))} [{ev.get("kind")}] {ev.get("stage") or ""} {ev.get("message") or ""}'
            add(line.rstrip())
            detail = ev.get('detail')
            if isinstance(detail, dict) and detail.get('traceback'):
                add('    ' + detail['traceback'].rstrip().replace('\n', '\n    '))

    if log_path:
        lines = _tail(log_path, LOG_LINES)
        if lines:
            add('')
            add(f'--- last {len(lines)} log lines ---')
            out.extend(lines)
    return _private('\n'.join(out)) + '\n'
