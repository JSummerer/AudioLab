"""yt-dlp integration: probing links, downloading audio/video, thumbnails,
and keeping the downloader itself up to date.

yt-dlp always runs as `python -m yt_dlp` from AudioLab's own environment (no
PATH lottery), with our ffmpeg directory and — for YouTube — the deno
JavaScript runtime that recent YouTube extraction requires.
"""

import importlib.metadata
import json
import re
import shutil
import sys
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..context import Progress
from ..errors import AudioLabError, Cancelled, DownloadError
from ..settings import LIMITS, SETTINGS
from ..util import human_duration, unique_path
from . import export, proc
from .ffmpeg import ffmpeg_dir

_URL_RE = re.compile(r'^https?://[^\s/$.?#][^\s]*$', re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r'^(?:www\.|m\.|music\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*$', re.IGNORECASE)
_PROGRESS_TAG = '[al-progress]'
_FILE_TAG = 'AL_FILE '
_PROGRESS_EVERY_S = 0.3
_THUMBNAIL_MAX_BYTES = 15 * 1024 * 1024
_THUMBNAIL_TIMEOUT_S = 30
_THUMBNAIL_SOCKET_TIMEOUT_S = 20


# ---------------------------------------------------------------------------
# URLs & titles
# ---------------------------------------------------------------------------
def normalize_url(text: str) -> str | None:
    """'youtu.be/abc' → 'https://youtu.be/abc'; returns None for non-links."""
    t = (text or '').strip()
    if not t or any(c.isspace() for c in t):
        return None
    if _URL_RE.match(t):
        return t
    if _BARE_DOMAIN_RE.match(t):
        return 'https://' + t
    return None


def is_url(text: str) -> bool:
    return normalize_url(text) is not None


_JUNK_WORDS = {
    'official',
    'music',
    'video',
    'audio',
    'lyric',
    'lyrics',
    'visualizer',
    'visualiser',
    'hd',
    'hq',
    '4k',
    '8k',
    'uhd',
    '1080p',
    '720p',
    '2160p',
    '60fps',
    '50fps',
    'full',
    'set',
    'mv',
    'clip',
    'videoclip',
    'version',
    'with',
    'w/',
    'free',
    'download',
    'out',
    'now',
    '&',
    '-',
    '|',
    '+',
}


def _is_junk_bracket(content: str) -> bool:
    words = re.findall(r'[\w/&+|-]+', content.lower())
    return bool(words) and all(w in _JUNK_WORDS for w in words)


def clean_title(title: str) -> str:
    """Strip upload noise like '(Official Video)', '[4K 60FPS]', '(Lyrics)' while
    keeping meaningful brackets such as '(Live at Wembley)' or '(Remix)'."""
    original = title or ''
    cleaned = re.sub(
        r'\s*[\[(]([^\[\]()]*)[\])]', lambda m: '' if _is_junk_bracket(m.group(1)) else m.group(0), original
    )
    # Trailing ' | Official Audio' style segments
    segments = re.split(r'\s+\|\s+', cleaned)
    while len(segments) > 1 and _is_junk_bracket(segments[-1]):
        segments.pop()
    cleaned = ' | '.join(segments)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'\s*[-–—|]\s*$', '', cleaned)
    cleaned = re.sub(r'^\s*[-–—|]\s*', '', cleaned)
    return cleaned.strip() or original.strip()


def _clean_uploader(uploader: str | None) -> str:
    u = (uploader or '').strip()
    u = re.sub(r'\s*-\s*Topic$', '', u)
    u = re.sub(r'VEVO$', '', u).strip()
    u = re.sub(r'\s*\((?:official|offizieller)?\s*channel\)$', '', u, flags=re.IGNORECASE)
    return u.strip()


def split_artist_title(title: str, uploader: str | None = None) -> tuple[str, str]:
    """'Artist - Song (Official Video)' → ('Artist', 'Song'). Falls back to the
    (cleaned) uploader as artist when the title has no 'Artist - ' prefix."""
    t = clean_title(title)
    m = re.match(r'^(.+?)\s+[-–—]\s+(.+)$', t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return _clean_uploader(uploader), t


def link_names(info: dict[str, Any]) -> tuple[str | None, str]:
    """(artist, song title) of a link — the one rule for the probe card and the job's file names and
    tags. `info` is a `probe()` result (older ones lack the music fields; then the title decides).

    yt-dlp's music fields (`track` with `artists`: auto-generated "Topic" uploads, a video's music
    section) count only when the video is that one track — its title names the track. A set or mix
    whose music section lists a licensed song keeps its own name. Otherwise 'Artist - Title' in the
    title, otherwise the uploader (without ' - Topic' / 'VEVO')."""
    raw_title = info.get('raw_title') or info.get('title') or ''
    track = str(info.get('music_track') or '').strip()
    artists = [str(a).strip() for a in info.get('music_artists') or [] if str(a).strip()]
    if track and artists and re.search(rf'(?<!\w){re.escape(track.casefold())}(?!\w)', raw_title.casefold()):
        return ', '.join(dict.fromkeys(artists)), track
    artist, title = split_artist_title(raw_title, info.get('uploader'))
    return artist or None, title


def _setlist_entries(text: str) -> list[dict[str, Any]]:
    """Timestamped lines in the order they appear in the text."""
    tracks = []
    pattern = re.compile(
        r'(?:^|\n)\s*(?:[\d]+[.)]\s*)?(?:[\[(])?((?:(\d{1,2}):)?(\d{1,2}):(\d{2}))(?:[\])])?\s*(?:[-–—:]\s*)?(.*)'
    )
    for match in pattern.finditer(text or ''):
        ts_str, hh, mm, ss, name = match.groups()
        if not name or not name.strip():
            continue
        total = (int(hh) if hh else 0) * 3600 + int(mm) * 60 + int(ss)
        tracks.append({'start_s': total, 'timestamp': ts_str, 'title': name.strip()})
    return tracks


_MIN_TRACK_S = 1.0


def _track_title(raw: str, n: int) -> str:
    t = re.sub(r'^(?:[\s\-–—|:]*(?:\d{1,2}:)?\d{1,2}:\d{2}\b)+', '', raw or '')  # '00:00 - 03:20 Track' ranges
    t = re.sub(r'^[\s\-–—|:•·▶►▸*#]+', '', t)
    t = re.sub(r'^\d{1,3}[.)]\s+', '', t)  # '01. Track'
    t = clean_title(re.sub(r'^["“](.*)["”]$', r'\1', t.strip())).strip()  # '"Title"', not "'Til Tuesday"
    return t[:200] or f'Track {n}'


def make_tracks(
    chapters: list[dict[str, Any]] | None, description: str | None, duration_s: float | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn chapters (preferred) or a timestamped tracklist in the description into
    contiguous tracks [{n, title, start_s, end_s}]. Returns ([], None) when nothing usable.

    A description list must have ≥3 strictly increasing timestamps inside the
    duration (the longest such run is used, so a stray '3:45' elsewhere in the text
    does not spoil it); a lead-in of up to 30 s before the first entry joins track 1."""
    dur = float(duration_s) if duration_s else None
    starts: list[tuple[float, str]] = []
    source = None
    chs = [c for c in (chapters or []) if isinstance(c, dict)]
    if len(chs) >= 2:
        starts = sorted(((float(c.get('start_s') or 0), c.get('title') or '') for c in chs), key=lambda c: c[0])
        if dur is None:
            dur = max((float(c.get('end_s') or 0) for c in chs), default=0.0) or None
        source = 'chapters'
    elif dur:
        runs: list[list[dict[str, Any]]] = []
        for e in _setlist_entries(description or ''):
            if e['start_s'] >= dur:
                continue
            if runs and e['start_s'] > runs[-1][-1]['start_s']:
                runs[-1].append(e)
            else:
                runs.append([e])
        best = max(runs, key=len, default=[])
        if len(best) >= 3:
            starts = [(float(e['start_s']), e['title']) for e in best]
            if starts[0][0] <= 30:
                starts[0] = (0.0, starts[0][1])
            source = 'description'
    kept: list[tuple[float, str]] = []
    for start, title in starts:
        if dur is not None and start > dur - _MIN_TRACK_S:
            break
        if kept and start - kept[-1][0] < _MIN_TRACK_S:
            continue  # zero-length entry
        kept.append((start, title))
    tracks = []
    for i, (start, title) in enumerate(kept):
        end = kept[i + 1][0] if i + 1 < len(kept) else dur
        tracks.append(
            {
                'n': i + 1,
                'title': _track_title(title, i + 1),
                'start_s': round(start, 3),
                'end_s': round(end, 3) if end is not None else None,
            }
        )
    if len(tracks) < 2:
        return [], None
    return tracks, source


# A time anywhere on a line: '3:15', '03:15', '1:02:03', '75:30' (minutes past the hour), optionally
# in brackets; digits or colons right next to it mean it is something else.
_LINE_TIME_RE = re.compile(r'[\[(]?(?<![\d:.])(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?![\d:])[\])]?')
_EDGE_SEP_RE = re.compile(r'^[\s\-–—|:•·▶►▸*#,;)\]]+|[\s\-–—|:@,;(\[]+$')
_NUMBERING_RE = re.compile(r'\d{1,3}[.)]?')  # '1.' / '01' in front of the time
_RANGE_END_RE = re.compile(r'^\s*[-–—~]\s*[\[(]?(?:\d{1,2}:)?\d{1,3}:\d{2}[\])]?(?![\d:])')
TRACKLIST_MAX_CHARS = 50_000


def parse_tracklist(text: str, duration_s: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strictly parse a tracklist the user pasted — one track per line, its start time anywhere on
    the line ('0:00 Artist - Title', '[00:00:00] Title', '1. 03:15 Title', 'Title 03:15').

    Returns (tracks, errors): tracks in the `make_tracks` shape [{n, title, start_s, end_s}] built
    from the good lines, errors as [{line, message}] (1-based line numbers, plain words). Unlike
    `make_tracks` nothing is guessed: no line is skipped silently and no start is moved. A first
    start after 0:00 stays where it is — the audio before it belongs to no track (the full file
    still has it). One line is a valid (single-track) list; the last track runs to `duration_s`."""
    dur = float(duration_s) if duration_s else None
    errors: list[dict[str, Any]] = []
    kept: list[tuple[float, str, int, str]] = []  # (start, raw title, line, time as typed)
    for i, line in enumerate((text or '').splitlines(), start=1):
        if not line.strip():
            continue
        m = _LINE_TIME_RE.search(line)
        if not m:
            errors.append({'line': i, 'message': 'no time found (expected e.g. "12:30 Artist - Title")'})
            continue
        hh, mm, ss = m.groups()
        shown = m.group(0).strip('[]()')
        if int(ss) > 59 or (hh is not None and int(mm) > 59):
            errors.append({'line': i, 'message': f'{shown} is not a valid time'})
            continue
        start = float((int(hh) if hh else 0) * 3600 + int(mm) * 60 + int(ss))
        if kept and start <= kept[-1][0]:
            prev = kept[-1]
            errors.append(
                {
                    'line': i,
                    'message': f'{shown} is the same time as line {prev[2]}'
                    if start == prev[0]
                    else f'time goes backwards ({shown} comes after {prev[3]} on line {prev[2]})',
                }
            )
            continue
        if dur is not None and start > dur - _MIN_TRACK_S:
            where = 'after the end' if start >= dur else 'at the very end'
            errors.append({'line': i, 'message': f'{shown} is {where} ({human_duration(dur)})'})
            continue
        # The title is the line without the time: text on both sides counts ('Artist - 03:15 Track',
        # 'Title [03:15] (Live)'); only numbering in front and the end of a range ('- 05:20') go.
        before = line[: m.start()]
        after = _RANGE_END_RE.sub('', line[m.end() :], count=1)
        if _NUMBERING_RE.fullmatch(_EDGE_SEP_RE.sub('', before)):
            before = ''
        kept.append((start, _EDGE_SEP_RE.sub('', ' '.join(f'{before} {after}'.split())), i, shown))
    tracks = []
    for n, (start, title, _, _) in enumerate(kept, start=1):
        end = kept[n][0] if n < len(kept) else dur
        tracks.append({'n': n, 'title': _track_title(title, n), 'start_s': start, 'end_s': end})
    return tracks, errors


def tracklist_problem(errors: list[dict[str, Any]]) -> str:
    """The parser's errors as one readable sentence ('line 7: time goes backwards …; line 9: …')."""
    shown = '; '.join(f'line {e["line"]}: {e["message"]}' for e in errors[:3])
    return shown + (f' (and {len(errors) - 3} more)' if len(errors) > 3 else '')


# ---------------------------------------------------------------------------
# yt-dlp invocation
# ---------------------------------------------------------------------------
def ytdlp_version() -> str | None:
    try:
        return importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        res = proc.run([sys.executable, '-m', 'yt_dlp', '--version'], timeout_s=30, what='downloader check')
        return res.stdout.strip() or None if res.returncode == 0 else None
    except Exception:
        return None


def _deno_path() -> str | None:
    try:
        import deno

        p = deno.find_deno_bin()
        return str(p) if p and Path(p).exists() else None
    except Exception:
        return shutil.which('deno')


def ytdlp_cmd() -> list[str]:
    """Base yt-dlp command line with AudioLab's tool locations."""
    cmd = [
        sys.executable,
        '-m',
        'yt_dlp',
        '--ignore-config',
        '--color',
        'never',
        '--socket-timeout',
        '30',
        '--ffmpeg-location',
        ffmpeg_dir(),
    ]
    deno = _deno_path()
    if deno:
        cmd += ['--js-runtimes', f'deno:{deno}']
    browser = SETTINGS.get().cookies_browser
    if browser in BROWSERS:
        cmd += ['--cookies-from-browser', browser]
    return cmd


_UPDATE_HINT = 'Update the downloader: Settings → Downloads & sign-in → "Update downloader".'
_SIGN_IN_HINT = (
    'Settings → Downloads & sign-in → "YouTube sign-in": pick the browser you use YouTube in '
    '(Firefox works most reliably).'
)
BROWSERS = {
    'firefox': 'Firefox',
    'chrome': 'Chrome',
    'edge': 'Edge',
    'brave': 'Brave',
    'safari': 'Safari',
    'chromium': 'Chromium',
    'opera': 'Opera',
    'vivaldi': 'Vivaldi',
}
# A server error at the site (HTTP 5xx: "Bad Gateway", "Service Unavailable", "Unable to download webpage:
# HTTP Error 503 …"): nothing to update or sign in for — only time helps.
_SERVER_TROUBLE = re.compile(
    r'http error 5\d\d|bad gateway|service unavailable|gateway time-?out|internal server error'
)
# yt-dlp messages that mean the browser's cookies could not be read (locked/encrypted/missing).
_COOKIE_READ_ERRORS = (
    'cookies database',
    'cookie database',
    'failed to load cookies',
    'decrypt',
    'dpapi',
    'keyring',
    'keychain',
    'find-generic-password',
    'binarycookies',
    'containers.json',
    'secretstorage',
    'kwallet',
    'local state file',
    'unsupported browser',
    'unknown browser',
)


def _cookie_read_error(browser: str, detail: str, raw: str | None = None) -> DownloadError:
    name = BROWSERS.get(browser, browser)
    hints = []
    if re.search(r'could not find \w+ cookies database|could not find local state', detail):
        hints = [f'Check that {name} is installed on this computer and you are signed in to YouTube there.']
    elif browser == 'safari':
        hints = [
            'Safari only shares its sign-in with apps that have "Full Disk Access": allow AudioLab '
            '(or your terminal) in System Settings → Privacy & Security.'
        ]
    elif 'keychain' in detail or 'find-generic-password' in detail:
        hints.append('If your Mac asks for keychain access, choose "Always Allow".')
    if browser != 'firefox':
        hints.append('Firefox works most reliably: Settings → Downloads & sign-in → "YouTube sign-in".')
    return DownloadError(
        f'Could not read the YouTube sign-in from {name} — close {name} and try again, or pick another browser.',
        hints=hints,
        fix='use_cookies',
        detail=raw,
    )


def _friendly_error(stderr: str, url: str) -> DownloadError:
    """Translate yt-dlp output into a message a non-technical user understands; yt-dlp's own words
    go along as `detail` (for bug reports), never into the message."""
    text = stderr or ''
    low = text.lower()
    err_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith('ERROR:')]
    raw = err_lines[-1][6:].strip() if err_lines else (text.strip().splitlines() or [''])[-1].strip()
    raw = re.sub(r'^\[[^\]]+\]\s*[\w-]+:\s*', '', raw)[:300]

    def e(msg, hints=None, fix=None):
        return DownloadError(msg, hints=hints, fix=fix, detail=raw or None)

    browser = SETTINGS.get().cookies_browser if SETTINGS.get().cookies_browser in BROWSERS else ''
    name = BROWSERS.get(browser, '')
    cookie_trouble = bool(browser) and any(m in low for m in _COOKIE_READ_ERRORS)  # incl. warnings
    if cookie_trouble and any(m in ' '.join(err_lines).lower() for m in _COOKIE_READ_ERRORS):
        return _cookie_read_error(browser, low, raw or None)
    if 'larger than max-filesize' in low:
        return e(
            f'This file is larger than the {int(LIMITS.max_download_mb)} MB download limit.',
            [f'AudioLab is set to download at most {int(LIMITS.max_download_mb)} MB per file: pick a shorter video.'],
        )
    if 'private video' in low:
        return e(
            'This video is private, so it cannot be downloaded.', ['Ask the uploader to make it public or unlisted.']
        )
    signed_in = (
        'not a bot' in low
        or 'confirm your age' in low
        or 'age-restricted' in low
        or 'inappropriate for some users' in low
        or 'members-only' in low
        or 'join this channel' in low
        or 'members only' in low
    )
    if signed_in and cookie_trouble:  # the sign-in was not readable, so YouTube saw a stranger
        return _cookie_read_error(browser, low, raw or None)
    if _SERVER_TROUBLE.search(low):  # before the checks below: "Service Unavailable" says "unavailable"
        return e(
            'The site is having trouble right now. Try again in a few minutes.',
            ['If it keeps happening, check that the link opens in your browser.'],
            fix='retry',
        )
    if 'http error 401' in low:
        if browser:
            return e(
                f'The site refused the download, even with your {name} sign-in.',
                [f'Check that you are signed in to that site in {name}, then try again.'],
            )
        return e(
            'The site only allows this download when you are signed in.',
            [_SIGN_IN_HINT, 'Or try another upload of the same audio.'],
            fix='use_cookies',
        )
    if 'not a bot' in low:
        if browser:
            return e(
                f"YouTube is still asking to confirm this isn't a bot, even with your {name} sign-in.",
                [
                    f'Open YouTube in {name}, check that you are signed in, then try again.',
                    'Or wait a while (often 30–60 minutes).',
                    _UPDATE_HINT,
                ],
                fix='update_downloader',
            )
        return e(
            "YouTube is asking to confirm this isn't a bot, so the download was blocked.",
            [_SIGN_IN_HINT, 'Or wait a while (often 30–60 minutes) and try again.', _UPDATE_HINT],
            fix='use_cookies',
        )
    if 'confirm your age' in low or 'age-restricted' in low or 'inappropriate for some users' in low:
        if browser:
            return e(
                f'This video is age-restricted and YouTube did not accept your {name} sign-in.',
                [
                    f'Check that you are signed in to YouTube in {name} with an account that may watch it.',
                    'Or try another upload of the same audio.',
                ],
            )
        return e(
            'This video is age-restricted — YouTube only plays it for signed-in adults.',
            [_SIGN_IN_HINT, 'Or try another upload of the same audio.'],
            fix='use_cookies',
        )
    if 'members-only' in low or 'join this channel' in low or 'members only' in low:
        if browser:
            return e(
                f'This video is for channel members only, and your {name} sign-in has no access to it.',
                ['Try another upload of the same audio.'],
            )
        return e(
            'This video is for channel members only.',
            [f'If you are a member: {_SIGN_IN_HINT}', 'Or try another upload of the same audio.'],
            fix='use_cookies',
        )
    if (
        'in your country' in low
        or 'geo restrict' in low
        or 'geo-restrict' in low
        or 'blocked it in your country' in low
    ):
        return e('This video is not available in your country.', ['Try another upload of the same audio.'])
    if (
        'premieres in' in low
        or 'live event will begin' in low
        or 'this live event' in low
        or 'is_upcoming' in low
        or 'is live' in low
        and 'stream' in low
    ):
        return e(
            'This is a live stream or premiere that has not finished yet.',
            ['Wait until the stream has ended and the video is available, then try again.'],
        )
    if (
        'video unavailable' in low
        or 'is unavailable' in low
        or 'has been removed' in low
        or 'no longer available' in low
        or 'account associated with this video has been terminated' in low
        or 'does not exist' in low
        or 'video not found' in low
        or 'http error 404' in low
    ):
        return e(
            'This video is unavailable — it may have been removed, or the link is wrong.',
            ['Check that the link opens in your browser.'],
        )
    if 'unsupported url' in low:
        return e(
            'This link is not supported. Paste a link to a video or audio page (for example a YouTube video).',
            ['Check that the link opens a video in your browser.'],
        )
    if 'is not a valid url' in low or 'incomplete youtube id' in low or 'invalid url' in low:
        return e('This does not look like a valid link.', ['Copy the full link from the browser address bar.'])
    if 'http error 429' in low or 'too many requests' in low:
        return e(
            'The site is limiting downloads right now (too many requests).',
            ['Wait 15–30 minutes and try again.'],
            fix='retry',
        )
    if 'http error 403' in low:
        return e(
            'The site refused the download (HTTP 403). This usually means the downloader needs an update.',
            [_UPDATE_HINT, 'Then try again.'],
            fix='update_downloader',
        )
    if any(
        s in low
        for s in (
            'getaddrinfo',
            'name or service not known',
            'nodename nor servname',
            'temporary failure in name resolution',
            'network is unreachable',
            'connection refused',
            'connection reset',
            'timed out',
            'failed to resolve',
            'no route to host',
            'urlopen error',
            'ssl:',
            'certificate verify failed',
        )
    ):
        return e(
            'Could not reach the site. Check your internet connection.',
            ['Make sure you are online, then try again.'],
            fix='retry',
        )
    if any(
        s in low
        for s in (
            'signature extraction failed',
            'nsig extraction failed',
            'unable to extract',
            'requested format is not available',
            'n challenge',
            'js runtime',
            'javascript runtime',
            'no video formats found',
            'only images are available',
            'please report this issue',
            'extractor error',
        )
    ):
        return e(
            "The site changed something and the downloader can't read it right now.",
            [
                _UPDATE_HINT,
                'If it still fails after updating, the site may need a fix upstream — try again in a day or two.',
            ],
            fix='update_downloader',
        )
    return e(  # unknown: not worth an automatic downloader update (fix None), the raw text is the detail
        "The download didn't work.",
        [
            'Check that the link opens in your browser, and try again later.',
            'If it keeps happening, update the downloader: Settings → Downloads & sign-in → "Update downloader".',
        ],
    )


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def _entry_url(entry: dict[str, Any]) -> str | None:
    for key in ('webpage_url', 'url'):
        u = entry.get(key)
        if isinstance(u, str) and u.startswith(('http://', 'https://')):
            return u
    vid = entry.get('id')
    if vid and (entry.get('ie_key') or '').lower().startswith('youtube'):
        return f'https://www.youtube.com/watch?v={vid}'
    return None


def probe(url: str, progress: Progress | None = None) -> dict[str, Any]:
    """Read metadata for a link without downloading. Single videos and
    playlists are both handled; playlists are listed flat and capped."""
    progress = progress or Progress.null()
    norm = normalize_url(url)
    if not norm:
        raise DownloadError('This does not look like a link. Paste a full web address (https://…).')
    progress(5, 'Reading link information…')
    cap = max(1, int(LIMITS.max_playlist_items))
    cmd = ytdlp_cmd() + ['-J', '--flat-playlist', '--no-playlist', '-I', f'1:{cap + 1}', '--', norm]
    res = proc.run(
        cmd,
        progress=progress,
        timeout_s=LIMITS.ytdlp_probe_timeout_s,
        what='link check',
        timeout_hint='Check your internet connection and try again.',
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise _friendly_error(res.stderr_tail, norm)
    text = res.stdout.strip()
    try:
        d = json.loads(text[text.find('{') :])
    except (json.JSONDecodeError, ValueError) as e:
        raise DownloadError(
            'The downloader returned unreadable information for this link.',
            hints=[_UPDATE_HINT],
            fix='update_downloader',
        ) from e
    progress(100)

    if d.get('_type') in ('playlist', 'multi_video') or 'entries' in d:
        entries = []
        for ent in d.get('entries') or []:
            if not isinstance(ent, dict):
                continue
            eu = _entry_url(ent)
            if not eu:
                continue
            title = ent.get('title') or eu
            entries.append(
                {
                    'url': eu,
                    'title': title,
                    'duration_s': float(ent['duration']) if ent.get('duration') else None,
                    'available': title not in ('[Private video]', '[Deleted video]'),
                }
            )
        total = d.get('playlist_count')
        total = int(total) if isinstance(total, (int, float)) and total > 0 else None
        truncated = len(entries) > cap or (total is not None and total > cap)
        entries = entries[:cap]
        return {
            'kind': 'playlist',
            'url': norm,
            'title': d.get('title') or 'Playlist',
            'uploader': d.get('uploader') or d.get('channel'),
            'count': total if total is not None else len(entries),
            'entries': entries,
            'truncated': truncated,
        }

    raw_title = d.get('title') or d.get('id') or 'Untitled'
    uploader = d.get('uploader') or d.get('channel') or d.get('creator')
    # Music uploads (YouTube "Topic" channels, a video's music section) carry track/artist fields.
    music_artists = d.get('artists') or d.get('creators') or ([d['artist']] if d.get('artist') else [])
    live_status = d.get('live_status')
    duration = float(d['duration']) if d.get('duration') else None
    chapters = [
        {
            'start_s': float(c.get('start_time') or 0),
            'end_s': float(c['end_time']) if c.get('end_time') is not None else None,
            'title': (c.get('title') or '').strip(),
        }
        for c in (d.get('chapters') or [])
        if isinstance(c, dict)
    ]
    tracks, tracks_source = make_tracks(chapters, d.get('description'), duration)
    out = {
        'kind': 'single',
        'url': norm,
        'id': d.get('id'),
        'title': clean_title(raw_title),
        'raw_title': raw_title,
        'music_track': d.get('track'),
        'music_artists': [str(a) for a in music_artists if a] if isinstance(music_artists, list) else [],
        'uploader': uploader,
        'duration_s': duration,
        'thumbnail': d.get('thumbnail'),
        'webpage_url': d.get('webpage_url') or norm,
        'extractor': d.get('extractor_key') or d.get('extractor'),
        'categories': list(d.get('categories') or []),
        'tags': list(d.get('tags') or [])[:50],
        'description': (d.get('description') or '')[:5000],
        'chapters': chapters,
        'tracks': tracks,
        'tracks_source': tracks_source,
        'upload_date': d.get('upload_date'),
        'is_live': bool(d.get('is_live')) or live_status in ('is_live', 'is_upcoming'),
    }
    out['artist'], out['track'] = link_names(out)  # the job names its files by the same rule
    q = parse_qs(urlparse(norm).query)
    if q.get('list'):
        out['playlist_url'] = f'https://www.youtube.com/playlist?list={q["list"][0]}'
    return out


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
def _fmt_bytes_per_s(v: float) -> str:
    for unit in ('B/s', 'KB/s', 'MB/s', 'GB/s'):
        if v < 1024:
            return f'{v:.1f} {unit}'
        v /= 1024.0
    return f'{v:.1f} TB/s'


def _fmt_eta(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60}:{s % 60:02d}'


def _num(token: str) -> float | None:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _download(
    url: str, dest_dir: Path, fmt_selector: str, stem: str, progress: Progress, extra: list[str], parts: int, label: str
) -> Path:
    norm = normalize_url(url)
    if not norm:
        raise DownloadError('This does not look like a link. Paste a full web address (https://…).')
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir).replace('%', '%%') + f'/{stem}.%(ext)s'
    tmpl = (
        f'download:{_PROGRESS_TAG} %(progress.downloaded_bytes)s|%(progress.total_bytes)s|'
        f'%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.status)s'
    )
    cmd = ytdlp_cmd() + [
        '--no-playlist',
        '--no-mtime',
        '--newline',
        '--progress',
        '--progress-template',
        tmpl,
        '--print',
        f'after_move:{_FILE_TAG}%(filepath)s',
        '--max-filesize',
        f'{int(LIMITS.max_download_mb)}M',
        '-f',
        fmt_selector,
        '-o',
        template,
        *extra,
        '--',
        norm,
    ]
    state = {'part': 0, 'last_pct': -1.0, 'last_emit': 0.0, 'files': [], 'finished_part': False}
    part_span = 95.0 / max(1, parts)

    def on_line(line: str):
        if line.startswith(_FILE_TAG):
            state['files'].append(line[len(_FILE_TAG) :].strip())
            return
        if line.startswith(_PROGRESS_TAG):
            fields = line[len(_PROGRESS_TAG) :].strip().split('|')
            if len(fields) < 6:
                return
            done, total, est, speed, eta, status = fields[:6]
            done_v = _num(done) or 0.0
            total_v = _num(total) or _num(est)
            pct = 100.0 * done_v / total_v if total_v else None
            if pct is not None and state['finished_part'] and pct < 50:
                state['part'] = min(parts - 1, state['part'] + 1)  # next stream (video, then audio)
                state['finished_part'] = False
            if status == 'finished' or (pct is not None and pct >= 99.9):
                state['finished_part'] = True
            now = time.monotonic()
            if now - state['last_emit'] < _PROGRESS_EVERY_S and status != 'finished':
                return
            state['last_emit'] = now
            bits = []
            sp = _num(speed)
            if sp:
                bits.append(_fmt_bytes_per_s(sp))
            et = _num(eta)
            if et is not None and status != 'finished':
                bits.append(f'{_fmt_eta(et)} left')
            pct_txt = f' {pct:.0f} %' if pct is not None else ''
            part_txt = f' (part {state["part"] + 1}/{parts})' if parts > 1 else ''
            msg = f'{label}{pct_txt}{part_txt}' + (f' · {" · ".join(bits)}' if bits else '')
            overall = state['part'] * part_span + (part_span * (pct or 0.0) / 100.0)
            try:
                progress(min(95.0, overall), msg)
            except Exception:
                pass  # cancellation is enforced by proc.run's poll loop
            return
        if line.startswith('[Merger]') or line.startswith('[VideoConvertor]') or line.startswith('[FixupM'):
            try:
                progress(96, 'Combining video and sound…')
            except Exception:
                pass

    progress(0, f'{label}…')
    before = {p.name for p in dest_dir.iterdir()}

    def drop_partials():
        for p in dest_dir.iterdir():
            if (
                p.name not in before
                and p.is_file()
                and (p.name.endswith(('.part', '.ytdl', '.temp')) or '.part-Frag' in p.name)
            ):
                p.unlink(missing_ok=True)

    try:
        res = proc.run(
            cmd,
            progress=progress,
            timeout_s=LIMITS.ytdlp_download_timeout_s,
            on_line=on_line,
            what='the download',
            timeout_hint='Check your internet connection and try again, or pick a shorter video.',
        )
    except BaseException:
        drop_partials()
        raise
    if res.returncode != 0:
        drop_partials()
        raise _friendly_error(res.stderr_tail + '\n' + res.stdout[-2000:], norm)

    for f in reversed(state['files']):
        p = Path(f)
        if p.is_file():
            progress(100)
            return p
    # Fallback: whatever new non-temp file appeared in the folder.
    new = [
        p
        for p in dest_dir.iterdir()
        if p.is_file()
        and p.name not in before
        and not p.name.startswith('.')
        and not p.name.endswith(('.part', '.ytdl', '.temp'))
    ]
    if new:
        progress(100)
        return max(new, key=lambda p: p.stat().st_mtime)
    if 'larger than max-filesize' in (res.stdout + res.stderr_tail).lower():
        raise _friendly_error('larger than max-filesize', norm)
    raise DownloadError(
        'The download finished but no file was produced.', hints=['Try again.', _UPDATE_HINT], fix='update_downloader'
    )


def download_audio(url: str, dest_dir: Path, progress: Progress | None = None) -> Path:
    """Download the best audio stream as-is (no re-encode). Returns the file path."""
    return _download(
        url,
        dest_dir,
        'bestaudio/best',
        'audio',
        progress or Progress.null(),
        extra=[],
        parts=1,
        label='Downloading audio',
    )


def download_video(url: str, dest_dir: Path, progress: Progress | None = None) -> Path:
    """Download video+sound as MP4 (H.264/AAC preferred for compatibility)."""
    return _download(
        url,
        dest_dir,
        'bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b',
        'video',
        progress or Progress.null(),
        extra=['--merge-output-format', 'mp4'],
        parts=2,
        label='Downloading video',
    )


def fetch_thumbnail(thumb_url: str, dest: Path, progress: Progress | None = None) -> Path:
    """Fetch and validate a small thumbnail, without replacing an existing file.

    Time/cancellation checks run between reads. The socket timeout bounds an in-flight
    operation; it does not make the overall deadline a strict interruption guarantee.
    """
    if not str(thumb_url or '').startswith(('http://', 'https://')):
        raise DownloadError('This link has no cover image.')
    progress = progress or Progress.null()
    deadline = time.monotonic() + _THUMBNAIL_TIMEOUT_S

    def check() -> None:
        progress.check_cancel()
        if time.monotonic() >= deadline:
            raise DownloadError('Getting the cover image took too long.', hints=['Try again later.'])

    check()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest)
    created = complete = False
    try:
        req = urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0 (AudioLab)'})
        with urllib.request.urlopen(req, timeout=_THUMBNAIL_SOCKET_TIMEOUT_S) as response:
            check()
            try:
                advertised = int(getattr(response, 'headers', {}).get('Content-Length', ''))
            except (TypeError, ValueError):
                advertised = 0
            if advertised > _THUMBNAIL_MAX_BYTES:
                raise DownloadError('The site sent a cover image larger than 15 MB.')
            with open(dest, 'xb') as target:
                created = True
                total = 0
                # HTTPResponse.read(n) can keep filling n bytes across many socket reads.
                # read1 returns after at most one raw read, so slow streams reach check().
                read_once = getattr(response, 'read1', None)
                read = read_once or response.read
                chunk_size = (1 << 16) if read_once else 1
                while True:
                    check()
                    chunk = read(min(chunk_size, _THUMBNAIL_MAX_BYTES - total + 1))
                    check()
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _THUMBNAIL_MAX_BYTES:
                        raise DownloadError('The site sent a cover image larger than 15 MB.')
                    target.write(chunk)
        if total < 100:
            raise DownloadError('The site sent an empty cover image.')
        try:
            export.check_picture(dest)  # before ffmpeg decodes it
        except AudioLabError as e:
            raise DownloadError(f"The site's cover image can't be used: {e}") from e
        progress.check_cancel()
        complete = True
        return dest
    except (OSError, ValueError) as e:
        raise DownloadError(
            'Could not get the cover image from the site.', hints=['Check your internet connection and try again.']
        ) from e
    finally:
        if created and not complete:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass  # preserve the original failure, including cancellation


def download_thumbnail(thumb_url: str, dest_jpg: Path, progress: Progress | None = None) -> Path | None:
    """Fetch a square JPEG cover (≤1000 px). Best effort, except job cancellation."""
    progress = progress or Progress.null()
    dest_jpg = Path(dest_jpg)
    raw = None
    try:
        raw = fetch_thumbnail(thumb_url, dest_jpg.with_name(dest_jpg.stem + '.thumb-src'), progress)
        progress.check_cancel()
        result = export.cover_jpeg(raw, dest_jpg, max_px=1000, square=True)
        progress.check_cancel()
        return result
    except Cancelled:
        raise
    except Exception:
        return None
    finally:
        if raw is not None:
            try:
                raw.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------
def update_ytdlp(progress: Progress | None = None) -> dict[str, Any]:
    """Upgrade yt-dlp (+ its YouTube helpers) inside AudioLab's environment."""
    progress = progress or Progress.null()
    before = ytdlp_version()
    progress(5, 'Updating the downloader…')
    spec = 'yt-dlp[default]'
    res = proc.run(
        [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-U', spec],
        progress=progress,
        timeout_s=600,
        what='downloader update',
        timeout_hint='Check your internet connection and try again.',
    )
    out_text = res.stdout + '\n' + res.stderr_tail
    if res.returncode != 0 and 'no module named pip' in out_text.lower():
        uv = shutil.which('uv')
        if uv:
            res = proc.run(
                [uv, 'pip', 'install', '--python', sys.executable, '-U', spec],
                progress=progress,
                timeout_s=600,
                what='downloader update',
                timeout_hint='Check your internet connection and try again.',
            )
            out_text = res.stdout + '\n' + res.stderr_tail
    ok = res.returncode == 0
    after = ytdlp_version()
    progress(100)
    if ok:
        msg = (
            f'Downloader updated: {before} → {after}.'
            if before != after
            else f'The downloader is already up to date ({after}).'
        )
    else:
        last = [ln for ln in out_text.splitlines() if ln.strip()][-1:] or ['unknown error']
        msg = f'Could not update the downloader: {last[0][:300]}'
    return {'ok': ok, 'before': before, 'after': after, 'message': msg}


def ytdlp_age_days(version: str | None) -> int | None:
    """Days since this yt-dlp version was released (its versions are dates), or None."""
    m = re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', version or '')
    try:
        return max(0, (date.today() - date(*map(int, m.groups()))).days) if m else None
    except ValueError:
        return None


_REPAIR_EVERY_S = 6 * 3600
_repair_lock = threading.Lock()
_last_repair: dict[str, Any] = {}  # {'at': monotonic end time, 'result': (before, after) | None}


def self_repair_update() -> tuple[str, str] | None:
    """Update yt-dlp after a 'site changed' failure, at most once per 6 h per process.
    Returns (before, after) when the version changed — the caller then retries once —
    else None. A job that waited here while another job ran the update gets that
    update's result. Deliberately not cancellable: killing pip mid-install can leave
    no downloader at all."""
    called = time.monotonic()
    with _repair_lock:
        last = _last_repair.get('at')
        if last is not None and last >= called:
            return _last_repair['result']
        if last is not None and called - last < _REPAIR_EVERY_S:
            return None
        try:
            res = update_ytdlp()
        except Exception:
            res = {'ok': False}
        changed = bool(res.get('ok') and res.get('after') and res.get('before') != res.get('after'))
        _last_repair.update(at=time.monotonic(), result=(res['before'], res['after']) if changed else None)
        return _last_repair['result']
