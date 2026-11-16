"""The update check: which release applies, what Health shows, and that nothing goes out unasked."""

import time

import pytest
from fastapi.testclient import TestClient

from audiolab import ai, updates
from audiolab.api import create_app
from audiolab.jobs import JobManager
from audiolab.media import export, ffmpeg, youtube
from audiolab.settings import SETTINGS


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """An app without host tools, AI probes or real jobs."""
    monkeypatch.setattr(youtube, 'ytdlp_version', lambda: '2026.08.19')
    monkeypatch.setattr(ffmpeg, 'ffmpeg_info', lambda: {'ok': True, 'source': 'bundled', 'version': '7.1'})
    monkeypatch.setattr(export, 'format_choices', lambda kind: [])
    monkeypatch.setattr(ai, 'ai_status', lambda refresh=False: {'available': False})

    def make():
        manager = JobManager(runner=lambda job, ctx: {}, stages_for=lambda job: [], root=tmp_path / 'jobs')
        return create_app(local=True, token=None, manager=manager)

    return make


def release(tag, draft=False):
    return {'tag_name': tag, 'draft': draft, 'prerelease': 'b' in tag or 'rc' in tag}


def test_versions_and_which_release_applies():
    key = updates.app_version_key
    assert key('2.4.0b9') < key('2.4.0rc1') < key('2.4.0') < key('v2.4.1') < key('2.10.0')
    assert key('nightly') is None and key('v2.5.0/../x') is None
    published = [release('v2.4.0b6'), release('v2.4.0b7'), release('v2.5.0b1'), release('v9.0.0', draft=True)]
    assert updates.newest_app_release(published, '2.4.0b6') == 'v2.5.0b1'  # a beta build is offered betas
    assert updates.newest_app_release([*published, release('v2.4.0')], '2.4.0') == 'v2.4.0'  # a final build is not
    assert updates.newest_app_release([release('2.6.0')], '2.5.0') == '2.6.0'  # plain X.Y.Z tags too


def published(tag, prerelease):
    """A row as GitHub's releases API returns it (only the fields the check reads, plus the title)."""
    title = f'AudioLab {tag[1:]}' + ('' if prerelease else ' (beta)')
    return {'tag_name': tag, 'name': title, 'draft': False, 'prerelease': prerelease}


RELEASE_2_5_0 = published('v2.5.0', prerelease=False)  # a normal release titled "(beta)"
BETA_2_KEPT = [RELEASE_2_5_0, published('v2.4.0b2', prerelease=True)]  # b2 hosts the AI pack
ALL_BETAS = [RELEASE_2_5_0, *(published(f'v2.4.0b{n}', prerelease=True) for n in range(6, 0, -1))]
PAGE_2_5_0 = 'https://github.com/JSummerer/AudioLab/releases/tag/v2.5.0'


@pytest.mark.parametrize('releases', [BETA_2_KEPT, ALL_BETAS], ids=['b2-kept', 'all-betas'])
@pytest.mark.parametrize(
    ('build', 'expected'),
    [
        ('2.4.0b6', {'latest': '2.5.0', 'available': True, 'url': PAGE_2_5_0}),
        ('2.5.0', {'latest': '2.5.0', 'available': False, 'url': PAGE_2_5_0}),  # up to date
    ],
)
def test_release_2_5_0_reaches_beta_builds_and_final_builds_are_up_to_date(monkeypatch, releases, build, expected):
    monkeypatch.setattr(updates, '__version__', build)
    monkeypatch.setattr(updates, 'fetch_json', lambda url: releases)
    monkeypatch.setattr(updates, 'ytdlp_release', lambda: {'version': '2026.10.26'})
    result = updates.check()
    assert result['app'] == expected and result['error'] is None


def test_a_final_build_is_never_offered_a_beta():
    betas = ALL_BETAS[1:]
    assert updates.newest_app_release(betas, '2.5.0') is None  # v2.4.0b2 … b6 are not offered to 2.5.0
    assert updates.newest_app_release([*betas, published('v2.6.0b1', True)], '2.5.0') is None


def test_check_caches_the_answer_and_health_only_reads_it(monkeypatch, app_factory):
    answers = {
        updates.RELEASES_API: [release('v2.4.0b6'), release('v99.0.0'), release('v99.1.0/evil')],
        updates.YTDLP_RELEASE_API: None,
    }
    monkeypatch.setattr(updates, 'fetch_json', lambda url: answers[url])
    monkeypatch.setattr(updates, 'ytdlp_release', lambda: {'version': '2099.01.01'})
    result = updates.check()
    assert result['app'] == {
        'latest': '99.0.0',
        'available': True,
        'url': 'https://github.com/JSummerer/AudioLab/releases/tag/v99.0.0',
    }
    assert result['error'] is None and result['checked_at'] and not result['enabled']

    def offline(url):
        raise updates.UpdateCheckError('No internet connection to GitHub. Connect and try again.')

    monkeypatch.setattr(updates, 'fetch_json', offline)
    failed = updates.check()
    assert failed['error'].startswith('No internet') and failed['app']['latest'] == '99.0.0'  # last answer kept
    monkeypatch.setattr(updates, 'fetch_json', lambda url: pytest.fail('Health went to the network'))
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        health = client.get('/api/health').json()
    assert health['updates'] == failed and health['downloader']['latest'] == '2099.01.01'


@pytest.mark.parametrize('enabled', [False, True])
def test_no_request_unless_switched_on(monkeypatch, app_factory, enabled):
    started = []
    monkeypatch.setattr(updates, 'ensure_scheduler', lambda on_checked=None: started.append(on_checked))
    monkeypatch.setattr(updates, 'fetch_json', lambda url: pytest.fail('A request went out'))
    SETTINGS.update({'check_updates': enabled})
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        assert client.get('/api/health').json()['updates']['enabled'] is enabled
    assert bool(started) is enabled  # on: the daily thread is started; the request itself happens there
    assert updates.due() is enabled  # never checked yet


def test_automatic_check_never_runs_more_than_once_a_day():
    SETTINGS.update({'check_updates': True})
    now = time.time()
    updates._save({'checked_at': now - 2 * 3600, 'error': 'No internet connection to GitHub. Connect and try again.'})
    assert not updates.due(now)  # a failed check waits for the next daily slot, too
    assert updates.due(now - 2 * 3600 + updates.CHECK_EVERY_S)


def test_once_a_day_holds_when_the_record_cannot_be_saved(monkeypatch):
    SETTINGS.update({'check_updates': True})
    calls = []
    monkeypatch.setattr(updates, 'fetch_json', lambda url: calls.append(url) or [])
    monkeypatch.setattr(updates, 'ytdlp_release', lambda: calls.append('ytdlp') or {'version': '2099.01.01'})

    def disk_full(*args):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(updates, 'replace_file', disk_full)
    updates.check()
    assert len(calls) == 2 and not updates._cache_path().exists()
    assert not updates.due()  # the next 10-minute tick asks nothing
    assert updates.due(time.time() + updates.CHECK_EVERY_S)


def test_only_github_addresses_are_followed():
    for url in ('http://api.github.com/x', 'https://example.com/x', 'https://github.com.evil.test/x'):
        with pytest.raises(updates.UpdateCheckError):
            updates.check_url(url)
    updates.check_url('https://release-assets.githubusercontent.com/github-production-release-asset/1/2')
