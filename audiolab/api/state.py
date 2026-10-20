"""Runtime state of one app instance (`app.state.audiolab`): the job manager, the live-event
hub, the AI installer, keep-awake, short-lived caches (health parts, link probes) and the
helpers several routes share. Routes receive it with the `State` dependency."""

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from .. import APP_NAME, __version__, ai, recipes, runtime, system, tasks
from ..ai.install import AIInstaller
from ..errors import AudioLabError
from ..jobs import Job, JobManager
from ..media import export, ffmpeg, youtube
from ..settings import LIMITS, SETTINGS
from .events import Hub

JOB_ID_RE = re.compile(r'^j_\d{8}_\d{6}_[0-9a-f]{6}$')
PROBE_TTL_S = 15 * 60
SHUTDOWN_TIMEOUT_S = 5.0


class AppState:
    def __init__(self, local: bool, token: str | None, manager: JobManager | None = None):
        self.local = local
        self.token = token
        self.manager = manager or JobManager(runner=tasks.run_job, stages_for=tasks.stages_for)
        self.hub = Hub()
        self.installer = AIInstaller()
        self.keep_awake = system.KeepAwake()
        self._caches: dict[str, tuple[float, Any]] = {}
        self._probes: dict[str, tuple[float, dict[str, Any]]] = {}
        self._probe_lock = threading.Lock()
        self.manager.on_running_changed = self.keep_awake.set
        self.manager.on_finished = self._notify_finished
        self.pcm_stop = threading.Event()  # set on quit: the compare players' decodes stop (ffmpeg.decode_pcm)

    def shutdown(self) -> None:
        """Stop all jobs (bounded, see JobManager.shutdown), let the computer sleep again and
        end the live-update streams and the compare players' decodes. Safe to call twice — the desktop
        window may call it on quit before the server stops. A running AI-pack install is left alone (see
        AIInstaller)."""
        self.pcm_stop.set()
        self.manager.shutdown(SHUTDOWN_TIMEOUT_S)
        self.keep_awake.set(False)
        self.hub.close()

    def _notify_finished(self, job: Job) -> None:
        if not self.local or not job.started_at or (job.finished_at or 0) - job.started_at < 20:
            return
        if job.status == 'done':
            system.notify(APP_NAME, f'{tasks.LABELS.get(job.task, job.task)} finished: {job.title}')
        elif job.status == 'failed':
            system.notify(APP_NAME, f'Failed: {job.title}')

    # ------------------------------------------------------------ for routes
    def job_or_404(self, job_id: str) -> Job:
        job = self.manager.get(job_id) if JOB_ID_RE.fullmatch(job_id or '') else None
        if job is None:
            raise HTTPException(404, 'Job not found.')
        return job

    def require_local(self) -> None:
        if not self.local:
            raise HTTPException(403, 'Folders can only be opened when AudioLab runs on this computer.')

    def reveal(self, folder: Path | None = None) -> dict[str, Any]:
        """Show a job's or entry's folder in Finder; when it is gone, the output folder instead (said
        with `fallback`). `None`: the output folder itself — made first when it isn't there yet (a new
        user's "Open output folder"), unless it can't be (a drive that isn't connected). A job's or
        entry's folder is never made again."""
        self.require_local()
        root = SETTINGS.output_dir
        if folder is not None and folder.is_dir():
            system.reveal(folder)
            return {'ok': True}
        if root.is_dir():
            system.reveal(root)
            return {'ok': True} if folder is None else {'ok': True, 'fallback': 'output'}
        if folder is not None:
            raise AudioLabError(
                f"This folder isn't there any more: {folder}.",
                hints=["It was moved or deleted, or it is on a drive that isn't connected."],
            )
        if system.folder_writable(root):
            root.mkdir(parents=True, exist_ok=True)  # an OSError here: api/app.py words it
            system.reveal(root)
            return {'ok': True}
        raise AudioLabError(
            f"The output folder isn't available: {root}.",
            hints=['Connect the drive it is on, or choose another folder in Settings → Files.'],
        )

    def reveal_app_folder(self, target: str) -> dict[str, Any]:
        """Reveal only the two app-owned folders offered by Settings, never a supplied path."""
        self.require_local()
        if target == 'data':
            folder = SETTINGS.data_dir
        elif target == 'logs':
            folder = system.log_dir(SETTINGS.data_dir)
        else:
            raise HTTPException(404, 'Unknown app folder.')
        if not folder.is_dir():
            raise AudioLabError('This app folder does not exist yet.', hints=['Use AudioLab, then try again.'])
        system.reveal(folder)
        return {'ok': True}

    def cached(self, key: str, fn: Callable[[], Any], ttl: float = 300.0) -> Any:
        now = time.time()
        hit = self._caches.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._caches[key] = (now, value)
        return value

    def forget(self, key: str) -> None:
        self._caches.pop(key, None)

    # ---------------------------------------------------------------- health
    def ai_health(self) -> dict[str, Any]:
        st = dict(ai.ai_status())
        st.update(self.installer.status())
        if not st.get('available') and not st.get('reason'):
            st['reason'] = 'AI features are not installed.'
        return st

    def downloader_health(self) -> dict[str, Any]:
        v = self.cached('ytdlp', youtube.ytdlp_version, 60)  # short: jobs may update it (self-repair)
        return {
            'ok': bool(v),
            'version': v,
            'age_days': youtube.ytdlp_age_days(v),
            'update_supported': not runtime.is_packaged(),
        }

    def health_data(self) -> dict[str, Any]:
        ff = self.cached('ffmpeg', ffmpeg.ffmpeg_info, 600)
        return {
            'app': APP_NAME,
            'version': __version__,
            'local': self.local,
            'packaged': runtime.is_packaged(),
            'ffmpeg': {k: ff.get(k) for k in ('ok', 'version', 'source', 'error')},
            'downloader': self.downloader_health(),
            'ai': self.ai_health(),
            'output_dir': str(SETTINGS.output_dir),
            'output_dir_ok': system.folder_writable(SETTINGS.output_dir),  # false: a drive that isn't connected…
            'upload_extensions': sorted(export.UPLOAD_EXT),
            'data_dir': str(SETTINGS.data_dir),
            'logs_dir': str(system.log_dir(SETTINGS.data_dir)),
            'limits': {
                'max_upload_mb': LIMITS.max_upload_mb,
                'max_duration_s': LIMITS.max_duration_s,
                'max_playlist_items': LIMITS.max_playlist_items,
            },
            'recipes': recipes.list_recipes(),
            'formats': {k: export.format_choices(k) for k in ('download', 'enhance', 'separate')},
            'format_notes': {'source': export.SOURCE_NOTE},
            'ai_models': ai.model_choices(),
        }

    def system_event(self) -> None:
        self.hub.publish('system', {'ai': self.ai_health(), 'downloader': self.downloader_health()})

    # ---------------------------------------------------------------- probes
    def probe_with_repair(self, url: str) -> dict[str, Any]:
        """Probe; on a 'site changed' error update the downloader once (if allowed) and retry."""
        try:
            return youtube.probe(url)
        except AudioLabError as e:
            if e.fix != 'update_downloader' or not SETTINGS.get().auto_update_downloader:
                raise
            if not youtube.self_repair_update():
                raise
            self.forget('ytdlp')
            self.system_event()
            return youtube.probe(url)

    def remember_probe(self, url: str, result: dict[str, Any]) -> None:
        """Keep a lookup for 15 minutes (jobs started from it skip a second one) — not a live stream or
        premiere: once it has ended, the next try must see that."""
        if result.get('is_live'):
            return
        with self._probe_lock:
            now = time.time()
            for k in [k for k, (t, _) in self._probes.items() if now - t > PROBE_TTL_S]:
                del self._probes[k]
            self._probes[url] = (now, result)
            if result.get('kind') == 'playlist':
                for e in result.get('entries', []):
                    self._probes[f'entry:{e["url"]}'] = (now, {'playlist_title': result.get('title'), **e})

    def cached_probe(self, key: str) -> dict[str, Any] | None:
        with self._probe_lock:
            hit = self._probes.get(key)
        return hit[1] if hit and time.time() - hit[0] < PROBE_TTL_S else None


def get_state(request: Request) -> AppState:
    return request.app.state.audiolab


State = Annotated[AppState, Depends(get_state)]
