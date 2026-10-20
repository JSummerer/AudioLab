"""Install the optional AI pack (torch + demucs) into the running environment.

The UI starts it with `AIInstaller` (a background thread with a short log tail);
`audiolab install-ai` runs the same `install_command()` in the terminal.
"""

import importlib
import importlib.util
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable

from .. import APP_NAME, system
from ..diagnostics import log_file

AI_PACKAGES = ['torch>=2.1', 'demucs>=4.1']
STALE_PID_S = 6 * 3600  # an older pid file is from a long-finished install (its pid may be reused)


def install_command() -> list[str]:
    """pip (or uv) command that installs the AI pack for this Python."""
    if importlib.util.find_spec('pip') is not None:
        return [sys.executable, '-m', 'pip', 'install', '--progress-bar', 'off', *AI_PACKAGES]
    uv = shutil.which('uv')
    if uv:
        return [uv, 'pip', 'install', '--python', sys.executable, *AI_PACKAGES]
    raise RuntimeError('Neither pip nor uv is available to install packages.')


class AIInstaller:
    """Runs the install in a daemon thread, keeping a log tail for the UI.

    Nothing waits for it, so quitting the app never blocks on an install. pip is not
    stopped on quit either (an interrupted install can leave a broken torch): it runs in
    its own process group and writes to `ai-install.log` next to the app log — not to a
    pipe, which would break when the app is gone — and finishes in the background.
    `ai-install.pid` keeps a restarted app from starting a second pip next to it."""

    def __init__(self):
        self.running = False
        self.ok: bool | None = None
        self.log: deque = deque(maxlen=40)
        self._lock = threading.Lock()

    @staticmethod
    def _paths():
        log = log_file().with_name('ai-install.log')
        return log, log.with_name('ai-install.pid')

    def _left_running(self) -> bool:
        """pip is still busy from before (the app was quit and opened again meanwhile)."""
        pid_file = self._paths()[1]
        try:
            if time.time() - pid_file.stat().st_mtime > STALE_PID_S:
                return False
            return system.process_alive(int(pid_file.read_text()))
        except (OSError, ValueError):
            return False

    def status(self) -> dict[str, object]:
        log = '\n'.join(self.log)
        installing = self.running or self._left_running()
        if installing and not self.running:
            try:
                log = '\n'.join(self._paths()[0].read_text(encoding='utf-8', errors='replace').splitlines()[-40:])
            except OSError:
                pass
        return {'installing': installing, 'install_ok': self.ok, 'install_log': log}

    def start(self, on_done: Callable[[bool], None]) -> bool:
        with self._lock:
            if self.running or self._left_running():
                return False
            self.running, self.ok = True, None
            self.log.clear()
        threading.Thread(target=self._run, args=(on_done,), daemon=True).start()
        return True

    def _run(self, on_done: Callable[[bool], None]) -> None:
        ok = False
        try:
            cmd = install_command()
            self.log.append('$ ' + ' '.join(cmd))
            path, pid_file = self._paths()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as out:  # earlier attempts stay readable above
                out.write(f'\n--- {time.strftime("%Y-%m-%d %H:%M:%S")} $ {" ".join(cmd)}\n')
                out.flush()
                start = out.tell()
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **system.own_process_group()
                )
                system.register_process_group(proc)
            pid_file.write_text(str(proc.pid))
            rest = ''
            with open(path, encoding='utf-8', errors='replace') as f:
                f.seek(start)
                while True:
                    finished = proc.poll() is not None
                    *lines, rest = (rest + f.read()).split('\n')
                    self.log.extend(line.rstrip()[-300:] for line in lines if line.strip())
                    if finished:
                        break
                    time.sleep(0.5)
            if rest.strip():
                self.log.append(rest.rstrip()[-300:])
            ok = proc.returncode == 0
            pid_file.unlink(missing_ok=True)
        except Exception as e:
            self.log.append(f'Error: {e}')
        importlib.invalidate_caches()
        self.ok, self.running = ok, False
        self.log.append(f'{APP_NAME}: AI features {"installed" if ok else "could not be installed"}.')
        on_done(ok)
