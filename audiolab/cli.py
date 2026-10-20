"""Command line: `audiolab` opens the app; subcommands run tasks directly.

audiolab                          open the app (window or browser)
audiolab serve --host 0.0.0.0     headless server (Docker / home server)
audiolab download URL --format mp3
audiolab enhance FILE_OR_URL --type auto --best --formats mp3_320 wav16
audiolab separate FILE_OR_URL --mode karaoke
audiolab analyze FILE_OR_URL --json
audiolab doctor                   check that everything is installed
audiolab update                   update the YouTube downloader (yt-dlp)
audiolab shortcut                 add the app to ~/Applications (macOS)
"""

import argparse
import json
import os
import platform
import secrets
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_SLUG, __version__
from .runtime import is_packaged
from .settings import ENV_PREFIX


# ------------------------------------------------------------------ progress
class ConsoleProgress:
    """Renders task progress as one updating line (TTY) or sparse log lines."""

    def __init__(self, stages, quiet: bool = False):
        self.labels = {k: label for k, label, _ in stages}
        self.tty = sys.stderr.isatty()
        self.quiet = quiet
        self.last = 0.0
        self.last_stage = None
        self.last_pct_logged = -10.0

    def __call__(self, stage: str, pct: float, message: str | None) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        label = self.labels.get(stage, stage)
        if self.tty:
            if now - self.last < 0.1 and stage == self.last_stage:
                return
            width = 24
            filled = int(width * pct / 100)
            bar = '█' * filled + '░' * (width - filled)
            line = f'\r{label:<18} {bar} {pct:5.1f}%  {(message or "")[:60]:<60}'
            sys.stderr.write(line)
            sys.stderr.flush()
        elif stage != self.last_stage or pct - self.last_pct_logged >= 10:
            print(f'[{pct:5.1f}%] {label}: {message or ""}', file=sys.stderr, flush=True)
            self.last_pct_logged = pct
        self.last, self.last_stage = now, stage

    def done(self) -> None:
        if self.tty and not self.quiet:
            sys.stderr.write('\n')


def _source_for(value: str) -> dict[str, Any]:
    from .media.youtube import is_url

    if is_url(value):
        return {'kind': 'url', 'url': value}
    p = Path(value).expanduser()
    if not p.is_file():
        sys.exit(f'Not a link and no such file: {value}')
    return {'kind': 'file', 'name': p.name, 'path': str(p.resolve()), 'size_bytes': p.stat().st_size}


def _run_task(task: str, value: str, options: dict[str, Any], args) -> int:
    from . import tasks
    from .context import TaskContext
    from .errors import AudioLabError, Cancelled
    from .settings import SETTINGS

    if getattr(args, 'out', None):
        os.environ[ENV_PREFIX + 'OUTPUT_DIR'] = str(Path(args.out).expanduser().resolve())
    spec = tasks.get(task)
    source = _source_for(value)
    if source['kind'] == 'url':
        from .media import youtube

        try:
            try:
                meta = youtube.probe(source['url'])
            except AudioLabError as e:
                if is_packaged() or e.fix != 'update_downloader' or not SETTINGS.get().auto_update_downloader:
                    raise
                print('The site seems to have changed — updating the downloader and trying again…', file=sys.stderr)
                if not youtube.self_repair_update():
                    raise
                meta = youtube.probe(source['url'])
        except AudioLabError as e:
            _print_error(e)
            return 1
        if meta.get('kind') == 'playlist':
            entries = meta.get('entries', [])
            print(f'Playlist "{meta.get("title")}" — {len(entries)} items', file=sys.stderr)
            code = 0
            item_options = {k: v for k, v in options.items() if k != 'meta'}  # one title for all = wrong
            for e in entries:
                rc = _run_task(task, e['url'], item_options, args)
                if rc == 130:
                    return 130  # cancellation stops the whole playlist, including nested playlists
                code |= rc
            return code
        source['_probe'] = meta
    try:
        opts = tasks.validate(task, options, source)
    except AudioLabError as e:
        _print_error(e)
        return 2

    work = SETTINGS.jobs_dir / (time.strftime('cli_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]) / 'work'
    stages = spec.stages(opts, source)
    printer = ConsoleProgress(stages, quiet=getattr(args, 'json', False))
    notes = []
    ctx = TaskContext(
        'cli', work, opts, stages, emit_state=printer, emit_note=lambda kind, msg, detail: notes.append((kind, msg))
    )
    try:
        result = spec.run(ctx, source)
    except KeyboardInterrupt:
        ctx.cancel()
        printer.done()
        print('Cancelled.', file=sys.stderr)
        return 130
    except Cancelled:
        printer.done()
        print('Cancelled.', file=sys.stderr)
        return 130
    except AudioLabError as e:
        printer.done()
        _print_error(e)
        return 1
    finally:
        if not getattr(args, 'keep', False):
            shutil.rmtree(work.parent, ignore_errors=True)
    printer.done()
    if not getattr(args, 'keep', False):  # working files are gone — only list durable outputs
        result['files'] = [
            f for f in result.get('files', []) if not str(f.get('path', '')).startswith(str(work.parent))
        ]

    if getattr(args, 'json', False):
        clean = {k: v for k, v in result.items() if not k.startswith('_')}
        print(json.dumps(clean, indent=2, default=str))
        return 0
    print()
    for line in result.get('summary', []):
        print(f'  • {line}')
    if getattr(args, 'verbose', False):
        for kind, msg in notes:
            if kind in ('decision', 'warning'):
                print(f'    [{kind}] {msg}')
    for f in result.get('files', []):
        if f.get('kind') != 'report':
            print(f'  → {f["path"]}')
    for w in result.get('warnings', []):
        print(f'  ! {w}')
    return 0


def _run_many(task: str, args, options: dict[str, Any]) -> int:
    """Run a task for each input in turn (Ctrl+C stops the whole batch)."""
    meta = {k: getattr(args, k) for k in ('title', 'artist') if getattr(args, k, None) is not None}
    if meta and len(args.inputs) > 1:
        sys.exit('--title/--artist only work with a single input.')
    if meta:
        options = {**options, 'meta': meta}
    if getattr(args, 'cookies_from_browser', None):
        from .settings import SETTINGS

        SETTINGS.get().cookies_browser = args.cookies_from_browser  # this run only, not saved
    code = 0
    for i, value in enumerate(args.inputs):
        if len(args.inputs) > 1 and not getattr(args, 'json', False):
            print(f'\n[{i + 1}/{len(args.inputs)}] {value}', file=sys.stderr)
        rc = _run_task(task, value, options, args)
        if rc == 130:
            return rc
        code = code or rc
    return code


def _print_error(e) -> None:
    print(f'\n✗ {e}', file=sys.stderr)
    for h in getattr(e, 'hints', []) or []:
        print(f'  – {h}', file=sys.stderr)


# ------------------------------------------------------------------ commands
def cmd_doctor(args) -> int:
    from .ai import ai_status
    from .desktop import DEFAULT_PORT, port_free
    from .media import ffmpeg, youtube
    from .resources import disk_free_bytes, memory_report
    from .settings import SETTINGS
    from .util import human_bytes

    if getattr(args, 'report', False):
        from .diagnostics import build_report, log_file

        v = youtube.ytdlp_version()
        health = {
            'local': True,
            'ffmpeg': ffmpeg.ffmpeg_info(),
            'ai': ai_status(),
            'downloader': {'version': v, 'age_days': youtube.ytdlp_age_days(v)},
        }
        print(build_report(health, log_path=log_file()))
        return 0

    ok = True

    def line(good: bool, name: str, detail: str, fix: str = ''):
        nonlocal ok
        ok &= good or name.startswith('AI')
        mark = '✓' if good else ('–' if name.startswith('AI') else '✗')
        print(f' {mark} {name:<14} {detail}')
        if not good and fix:
            print(f'   {"":<14} → {fix}')

    print(f'{APP_NAME} {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}')
    line(
        (3, 10) <= sys.version_info[:2] < (3, 14),
        'Python',
        platform.python_version(),
        'Install Python 3.10–3.13.',
    )
    ff = ffmpeg.ffmpeg_info()
    line(
        ff.get('ok'),
        'ffmpeg',
        f'{ff.get("version")} ({ff.get("source")})' if ff.get('ok') else str(ff.get('error')),
        'Reinstall AudioLab (it ships ffmpeg) or install ffmpeg from ffmpeg.org.',
    )
    v = youtube.ytdlp_version()
    line(bool(v), 'Downloader', f'yt-dlp {v}' if v else 'yt-dlp missing', f'Run: {APP_SLUG} update')
    import importlib.util

    has_deno = importlib.util.find_spec('deno') is not None
    line(
        has_deno,
        'JS runtime',
        'deno (needed for YouTube)' if has_deno else 'deno missing — many YouTube videos will fail',
        'pip install deno',
    )
    st = ai_status()
    line(
        bool(st.get('available')),
        'AI features',
        f'ready on {str(st.get("device")).upper()} (torch {st.get("torch_version")})'
        if st.get('available')
        else str(st.get('reason') or 'not installed'),
        'Not included in this core app.' if is_packaged() else f'Run: {APP_SLUG} install-ai (optional)',
    )
    mem = memory_report()
    line(True, 'Memory', f'{human_bytes(int(mem["limit_bytes"]))} ({mem["source"]})')
    out = SETTINGS.output_dir
    free = disk_free_bytes(out)
    line(free > 2 * 1024**3, 'Disk', f'{human_bytes(free)} free at {out}', 'Free some disk space.')
    line(True, 'Data folder', str(SETTINGS.data_dir))
    port_ok = port_free('127.0.0.1', DEFAULT_PORT)
    line(
        port_ok,
        'Port',
        f'{DEFAULT_PORT} ' + ('free' if port_ok else 'busy (the app will pick another one)'),
    )
    print('\nAll good!' if ok else '\nSome required parts are missing (see ✗ above).')
    return 0 if ok else 1


def cmd_update(args) -> int:
    from .media import youtube

    r = youtube.update_ytdlp()
    print(r.get('message') or r)
    return 0 if r.get('ok') else 1


def cmd_install_ai(args) -> int:
    import subprocess

    from .ai.install import install_command
    from .runtime import is_packaged

    if is_packaged():
        from .errors import AudioLabError

        raise AudioLabError('Open AudioLab Settings → AI & system to install or remove the optional AI add-on.')

    cmd = install_command()
    print('Installing AI software (torch + demucs). Model files download on first use…')
    return subprocess.call(cmd)


def _saved_token() -> str:
    """The auto-generated access token, kept in the data folder so it survives restarts
    (a NAS user should not have to dig it out of the logs after every reboot)."""
    from .settings import SETTINGS

    path = SETTINGS.data_dir / 'token'
    try:
        saved = path.read_text(encoding='utf-8').strip()
        if len(saved) >= 12:
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(12)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding='utf-8')
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def cmd_serve(args) -> int:
    from .api import is_loopback

    token = os.environ.get(ENV_PREFIX + 'TOKEN') or None
    if token and token.lower() == 'none':
        token = None
    elif not token and not is_loopback(args.host):
        token = _saved_token()
        print(f'\n  {APP_NAME} is reachable from other devices, so it is protected by an access token:')
        print(f'    {token}')
        print(f'  Open http://<this-computer>:{args.port}/?token={token}  (in Docker: use the mapped port, e.g. 8088)')
        print(f'  (It is saved in the data folder and stays the same after restarts. Set {ENV_PREFIX}TOKEN to choose')
        print(f'  your own, or {ENV_PREFIX}TOKEN=none to disable it.)\n')
    from .desktop import make_server

    make_server(args.host, args.port, token=token).run()
    return 0


def cmd_app(args) -> int:
    from .desktop import run_app

    return run_app(port=args.port, window=not args.browser, open_browser=not args.no_open)


def _app_command() -> tuple[list[str], Path | None, str]:
    """What the app shortcut runs, where, and what it says when that is gone: this folder's
    start.sh (it also installs updates) when running from the folder's own .venv,
    otherwise this Python (a pip/pipx install)."""
    folder = Path(__file__).resolve().parent.parent
    start = folder / 'start.sh'
    if start.is_file() and Path(sys.prefix).resolve() == (folder / '.venv').resolve():
        missing = (
            f'The {APP_NAME} folder is no longer at {folder}. Open it where it is now and double-click '
            'AudioLab.command (or run ./start.sh) to set up this app again.'
        )
        return [str(start), '--app'], folder, missing
    missing = (
        f'The Python environment {APP_NAME} was installed in is gone. Install it again, then run: {APP_SLUG} shortcut'
    )
    return [os.path.abspath(sys.executable), '-m', __package__, 'app'], None, missing  # the venv's own link


def cmd_shortcut(args) -> int:
    if is_packaged():
        print(f'This download is already a Mac app. Drag {APP_NAME}.app into Applications.', file=sys.stderr)
        return 1
    from . import system
    from .desktop import APP_ICON

    command, folder, missing = _app_command()
    try:
        app = system.install_shortcut(
            command, icon=APP_ICON, version=__version__, folder=folder, missing=missing, dest=args.dest
        )
    except NotImplementedError as e:
        print(f'{e} Start {APP_NAME} with "{APP_SLUG}" (or start.sh / start.bat) instead.', file=sys.stderr)
        return 1
    except FileExistsError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f'Added {app.name} to {app.parent}.')
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # e.g. `audiolab doctor > report.txt` on a cp1252 Windows
        try:
            if (getattr(stream, 'encoding', '') or '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog=APP_SLUG, description=f'{APP_NAME}: download, enhance, split and analyze audio.'
    )
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {__version__}')
    sub = parser.add_subparsers(dest='cmd')

    p = sub.add_parser('app', help='open the app (default)')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--browser', action='store_true', help='use the web browser even if a native window is available')
    p.add_argument('--no-open', action='store_true', help='do not open a browser automatically')
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser('shortcut', help=f'add {APP_NAME} to your Applications folder (macOS)')
    p.add_argument('--dest', type=Path, help=argparse.SUPPRESS)  # developers: write the app somewhere else
    p.set_defaults(fn=cmd_shortcut)

    p = sub.add_parser('serve', help='run the server only (Docker, home server)')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8765)
    p.set_defaults(fn=cmd_serve)

    def common(p):
        p.add_argument('inputs', nargs='+', metavar='input', help='one or more links or files')
        p.add_argument('--out', help='output folder (default: your AudioLab output folder)')
        p.add_argument('--title', help='name used for the files and tags (one input only)')
        p.add_argument('--artist', help='artist tag (one input only)')
        p.add_argument(
            '--cookies-from-browser',
            metavar='BROWSER',
            choices=['firefox', 'chrome', 'edge', 'brave', 'safari', 'chromium', 'opera', 'vivaldi'],
            help="use this browser's YouTube sign-in (age-restricted videos, bot checks)",
        )
        p.add_argument('--json', action='store_true', help='print the result as JSON')
        p.add_argument('--keep', action='store_true', help='keep the working files')
        p.add_argument('-v', '--verbose', action='store_true', help='show every automatic decision')

    p = sub.add_parser('download', help='save a link (or convert a file)')
    common(p)
    p.add_argument('--format', default='mp3', choices=['mp3', 'm4a', 'opus', 'flac', 'wav', 'original', 'video'])
    p.add_argument('--no-cover', action='store_true')
    p.add_argument(
        '--split-tracks', action='store_true', help='also save one file per track (video chapters / tracklist)'
    )
    p.set_defaults(
        fn=lambda a: _run_many(
            'download', a, {'format': a.format, 'embed_cover': not a.no_cover, 'split_tracks': a.split_tracks}
        )
    )

    p = sub.add_parser('enhance', help='make it sound better (auto-detects the type)')
    common(p)
    p.add_argument('--type', default='auto', choices=['auto', 'club', 'live', 'music', 'voice'])
    q = p.add_mutually_exclusive_group()
    q.add_argument('--light', action='store_true', help='volume only — evens out loudness, keeps the sound')
    q.add_argument('--best', action='store_true', help='use AI (slower, needs the AI features)')
    p.add_argument(
        '--split-tracks', action='store_true', help='also save one file per track (video chapters / tracklist)'
    )
    p.add_argument('--formats', nargs='+', default=None, help='e.g. mp3_320 wav16 alac16 flac24 aac_256')
    p.add_argument('--lufs', type=float, help='target loudness (e.g. -14)')
    p.add_argument('--ceiling', type=float, help='true-peak ceiling in dBTP (e.g. -1)')
    p.add_argument('--bass', type=float, help='bass boost in dB (0–6)')
    p.add_argument('--denoise', choices=['off', 'light', 'strong'])
    p.add_argument('--restore-highs', action='store_true', help='regenerate missing treble on lossy sources')
    p.add_argument('--sample-rate', type=int, choices=[44100, 48000, 96000])

    def run_enhance(a):
        adv = {
            'target_lufs': a.lufs,
            'tp_ceiling_db': a.ceiling,
            'bass_db': a.bass,
            'denoise': a.denoise,
            'hf_restore': 'on' if a.restore_highs else None,
            'sample_rate': a.sample_rate,
        }
        opts = {
            'recipe': a.type,
            'quality': 'best' if a.best else 'light' if a.light else 'standard',
            'split_tracks': a.split_tracks,
            'advanced': {k: v for k, v in adv.items() if v is not None},
        }
        if a.formats:
            opts['formats'] = a.formats
        return _run_many('enhance', a, opts)

    p.set_defaults(fn=run_enhance)

    p = sub.add_parser('separate', help='split vocals and instruments (AI)')
    common(p)
    p.add_argument('--mode', default='karaoke', choices=['karaoke', 'stems4'])
    p.add_argument('--format', default='wav16')
    p.add_argument('--model', default='auto')
    p.set_defaults(fn=lambda a: _run_many('separate', a, {'mode': a.mode, 'format': a.format, 'model': a.model}))

    p = sub.add_parser('analyze', help='check loudness, peaks and real quality')
    common(p)
    p.set_defaults(fn=lambda a: _run_many('analyze', a, {}))

    p = sub.add_parser('doctor', help='check the installation')
    p.add_argument('--report', action='store_true', help='print a full system report to paste into a bug report')
    p.set_defaults(fn=cmd_doctor)
    sub.add_parser('update', help='update the YouTube downloader').set_defaults(fn=cmd_update)
    sub.add_parser('install-ai', help='install the optional AI features').set_defaults(fn=cmd_install_ai)

    args = parser.parse_args(argv)
    if not getattr(args, 'fn', None):
        args = parser.parse_args(['app', *(argv or [])])
    from .errors import AudioLabError

    try:
        return int(args.fn(args) or 0)
    except AudioLabError as error:
        print(str(error), file=sys.stderr)
        for hint in error.hints:
            print(f'  {hint}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
