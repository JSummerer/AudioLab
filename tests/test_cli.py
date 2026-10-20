"""CLI batch boundaries, with offline fake tasks and isolated working folders."""

from types import SimpleNamespace

import pytest

from audiolab import cli, tasks
from audiolab.errors import AudioLabError, Cancelled
from audiolab.media import youtube


@pytest.mark.parametrize('cancel', [KeyboardInterrupt, Cancelled])
@pytest.mark.parametrize('earlier_failure', [False, True])
def test_playlist_cancellation_stops_nested_batch(monkeypatch, cancel, earlier_failure):
    started = []

    def probe(url):
        name = url.rsplit('/', 1)[-1]
        entries = {
            'playlist': (['failure'] if earlier_failure else []) + ['nested', 'outer-later'],
            'nested': ['cancel', 'inner-later'],
        }
        if name in entries:
            return {
                'kind': 'playlist',
                'title': name,
                'entries': [{'url': f'https://example.invalid/{item}'} for item in entries[name]],
            }
        return {'kind': 'single'}

    def run(ctx, source):
        name = source['url'].rsplit('/', 1)[-1]
        started.append(name)
        if name == 'failure':
            raise AudioLabError('Synthetic ordinary failure')
        if name == 'cancel':
            raise cancel()
        return {'files': [], 'summary': []}

    monkeypatch.setattr(youtube, 'probe', probe)
    monkeypatch.setattr(tasks, 'get', lambda _: SimpleNamespace(stages=lambda *_: [('test', 'Test', 1)], run=run))
    monkeypatch.setattr(tasks, 'validate', lambda *_: {})
    args = SimpleNamespace(inputs=['https://example.invalid/playlist', 'https://example.invalid/next-input'])
    assert cli._run_many('download', args, {}) == 130
    assert started == (['failure'] if earlier_failure else []) + ['cancel']


def test_playlist_ordinary_failure_still_continues(monkeypatch):
    started = []

    def probe(url):
        if url.endswith('/playlist'):
            return {
                'kind': 'playlist',
                'entries': [{'url': f'https://example.invalid/{name}'} for name in ('bad', 'good')],
            }
        return {'kind': 'single'}

    def run(ctx, source):
        started.append(source['url'].rsplit('/', 1)[-1])
        if started[-1] == 'bad':
            raise AudioLabError('Synthetic ordinary failure')
        return {'files': [], 'summary': []}

    monkeypatch.setattr(youtube, 'probe', probe)
    monkeypatch.setattr(tasks, 'get', lambda _: SimpleNamespace(stages=lambda *_: [('test', 'Test', 1)], run=run))
    monkeypatch.setattr(tasks, 'validate', lambda *_: {})
    assert cli._run_task('download', 'https://example.invalid/playlist', {}, SimpleNamespace()) == 1
    assert started == ['bad', 'good']
