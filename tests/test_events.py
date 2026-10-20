"""Live updates end when the app stops, so quitting never waits for open streams."""

import asyncio
import threading
import time

from audiolab.api.events import Hub
from audiolab.jobs import JobManager


def test_close_ends_streams_from_another_thread():
    async def main():
        hub = Hub()
        hub.loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        hub.clients.append(q)
        hub.publish('job', {'id': 1})
        threading.Thread(target=hub.close).start()  # the window quits on the main thread
        return hub, [await asyncio.wait_for(q.get(), 2) for _ in range(2)]

    hub, items = asyncio.run(main())
    assert items == [('job', {'id': 1}), None]  # the stream loop stops at None
    assert hub.closed


def test_shutdown_refuses_new_streams(tmp_path):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    mgr = JobManager(runner=lambda job, ctx: {}, stages_for=lambda job: [], root=tmp_path / 'jobs', workers=1)
    app = create_app(local=True, manager=mgr)
    with TestClient(app, base_url='http://127.0.0.1:8765') as c:
        hub = app.state.audiolab.hub
        app.state.audiolab.shutdown()
        deadline = time.monotonic() + 2
        while not hub.closed and time.monotonic() < deadline:  # closes on the server's loop
            time.sleep(0.02)
        assert c.get('/api/events').status_code == 503  # an open stream would never return
