// Settings → Help: the AudioLab update row, the downloader row and the automatic
// check switch, rendered from the production module (see settings-harness.mjs).
import assert from 'node:assert/strict';
import test from 'node:test';
import { Element, setup, text } from './settings-harness.mjs';

/** The wrapper of a spot (data-spot) and the state of its first status light. */
function spot(root, name) {
  if (root?.dataset?.spot === name) return root;
  for (const child of root?.children || []) {
    const found = spot(child, name);
    if (found) return found;
  }
  return null;
}
function light(root) {
  if (root?.led) return root.led;
  for (const child of root?.children || []) {
    if (!(child instanceof Element)) continue;
    const found = light(child);
    if (found) return found;
  }
  return null;
}

const packaged = (patch = {}) => ({
  version: '2.4.0', packaged: true, local: true, ffmpeg: { ok: true }, ai: {},
  downloader: { ok: true, version: '2026.10.26', age_days: 5, update_supported: true, source: 'bundled', bundled_version: '2026.10.26', latest: null, fallback: false },
  updates: { enabled: false, checking: false, checked_at: null, error: null, app: { latest: null, available: false, url: null } },
  ...patch,
});
const updates = (patch = {}, app = {}) => ({
  enabled: true, checking: false, checked_at: '2026-11-16T09:00:00Z', error: null,
  app: { latest: '2.4.0', available: false, url: 'https://github.com/owner/AudioLab/releases/tag/v2.4.0', ...app }, ...patch,
});

test('the AudioLab row: not checked, up to date, checking, failed', () => {
  const s = setup(packaged());
  const row = () => spot(s.body, 'updates');
  assert.match(text(row()), /Version 2\.4\.0 · not checked yet/);
  assert.equal(light(row()), 'dim');
  assert.equal(text(s.find('checkUpdates')), 'Check now');

  s.render(packaged({ updates: updates() }));
  assert.match(text(row()), /Version 2\.4\.0 · up to date/);
  assert.equal(light(row()), 'ok');
  assert.equal(s.find('getUpdate'), null, 'no link while up to date');

  s.render(packaged({ updates: updates({ checking: true }) }));
  assert.match(text(row()), /checking…/);
  assert.equal(s.find('checkUpdates')['aria-disabled'], 'true');
  s.find('checkUpdates').onclick();
  assert.equal(s.requests.length, 0, 'no second check while the server checks');

  s.render(packaged({ updates: updates({ error: 'GitHub could not be reached.' }) }));
  assert.match(text(row()), /couldn’t check/);
  assert.match(text(row()), /GitHub could not be reached\./);
  assert.equal(light(row()), 'dim');
  s.render(packaged({ updates: updates({ error: true }) }));
  assert.match(text(row()), /Try again later\./);
});

test('an available update links to its GitHub page with the replace note in the packaged app', () => {
  const url = 'https://github.com/owner/AudioLab/releases/tag/v2.5.0';
  const s = setup(packaged({ updates: updates({}, { latest: '2.5.0', available: true, url }) }));
  const link = s.find('getUpdate');
  assert.equal(link.tag, 'a');
  assert.equal(link.href, url);
  assert.equal(link.target, '_blank');
  assert.match(link.rel, /noopener/);
  assert.match(text(link), /Get 2\.5\.0/);
  // Check now stays (a later release can still be found; focus has somewhere to stay), as the minor action.
  assert.equal(text(s.find('checkUpdates')), 'Check now');
  assert.match(s.find('checkUpdates').class, /btn-ghost/);
  assert.match(link.class, /btn-key/);
  assert.match(s.text(), /2\.5\.0 is available/);
  assert.match(s.text(), /Quit AudioLab, then replace it in Applications\. Your Library and settings stay\./);
  // A source install gets the link but not the Applications note.
  s.render({ ...packaged({ updates: updates({}, { latest: '2.5.0', available: true, url }) }), packaged: false });
  assert.ok(s.find('getUpdate'));
  assert.doesNotMatch(s.text(), /replace it in Applications/);
});

test('the update link only ever points to https://github.com/', () => {
  for (const url of ['https://evil.example/AudioLab.dmg', 'http://github.com/owner/AudioLab', 'https://github.com.evil.io/x',
    'javascript:alert(1)', '', null, 42, { href: 'https://github.com/' }]) {
    const s = setup(packaged({ updates: updates({}, { latest: '2.5.0', available: true, url }) }));
    assert.equal(s.find('getUpdate'), null, `no link for ${JSON.stringify(url)}`);
    assert.ok(s.find('checkUpdates'));
    assert.match(s.text(), /2\.5\.0 is available/);
  }
});

test('Check now asks the server once, shows that it is checking, then shows the result', async () => {
  const s = setup(packaged());
  let finish;
  const calls = [];
  s.context.api = (path, options) => { calls.push({ path, ...options }); return new Promise((resolve) => { finish = resolve; }); };
  const pending = s.find('checkUpdates').onclick();
  assert.equal(text(s.find('checkUpdates')), 'Checking…');
  assert.equal(s.find('checkUpdates')['aria-disabled'], 'true');
  s.render(s.context.store.health); // a health event meanwhile keeps the pending state
  assert.equal(text(s.find('checkUpdates')), 'Checking…');
  await s.find('checkUpdates').onclick();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { path: '/api/system/check-updates', method: 'POST' });
  finish(updates({}, { latest: '2.5.0', available: true, url: 'https://github.com/owner/AudioLab/releases/latest' }));
  await pending;
  assert.ok(s.find('getUpdate'));
  assert.match(s.text(), /2\.5\.0 is available/);
  assert.equal(text(s.find('checkUpdates')), 'Check now', 'the pressed button survives the result');
  const again = s.find('checkUpdates').onclick();
  assert.equal(calls.length, 2, 'a later release can still be checked for');
  finish(updates());
  await again;
});

test('a failed check is reported and the button works again', async () => {
  const s = setup(packaged());
  const offline = new Error('Can’t reach the app.');
  s.context.api = async () => { throw offline; };
  await s.find('checkUpdates').onclick();
  assert.equal(s.errors[0], offline);
  assert.equal(text(s.find('checkUpdates')), 'Check now');
  assert.ok(!s.find('checkUpdates')['aria-disabled']);
});

test('Settings never contacts the network by itself, also while the server checks', () => {
  const s = setup(packaged());
  s.render(packaged({ updates: updates({ checking: true }) }));
  s.render(packaged({ updates: updates() }));
  assert.equal(s.requests.length, 0);
  assert.equal(s.scheduled.length, 0);
});

test('downloader: built in, updated with a way back, newer version, fallback', async () => {
  const s = setup(packaged());
  const row = () => spot(s.body, 'downloader');
  assert.match(text(row()), /Version 2026\.10\.26 · built in/);
  assert.ok(s.find('updateDownloader'));
  assert.equal(s.find('resetDownloader'), null);
  assert.equal(s.find('autoUpdateDownloader'), null, 'the packaged app never updates without a click');
  assert.doesNotMatch(s.text(), /Install a newer AudioLab app/);

  s.render(packaged({ downloader: { ok: true, version: '2026.9.1', age_days: 70, update_supported: true, source: 'bundled', latest: '2026.10.26' } }));
  assert.match(text(row()), /Version 2026\.9\.1 · built in · 2026\.10\.26 is available/);
  assert.match(text(row()), /70 days old.*“Update downloader”/);

  s.render(packaged({ downloader: { ok: true, version: '2026.11.10', age_days: 6, update_supported: true, source: 'updated', bundled_version: '2026.10.26', latest: '2026.11.10' } }));
  assert.match(text(row()), /Version 2026\.11\.10 · updated/);
  assert.doesNotMatch(text(row()), /is available/);
  assert.ok(s.find('resetDownloader'));
  await s.find('resetDownloader').onclick();
  assert.deepEqual(s.requests.at(-1), { path: '/api/system/downloader', method: 'DELETE' });

  s.render(packaged({ downloader: { ok: true, version: '2026.10.26', update_supported: true, source: 'bundled', bundled_version: '2026.10.26', fallback: true } }));
  assert.match(text(row()), /The update didn’t load\. Using built-in 2026\.10\.26\./);
  assert.ok(s.find('updateDownloader'));
  assert.ok(s.find('resetDownloader'), 'clears the failed update away');
});

test('keyboard focus stays in the row when "Use built-in version" goes away', async () => {
  const updated = { ok: true, version: '2026.11.10', age_days: 6, update_supported: true, source: 'updated', bundled_version: '2026.10.26', fallback: false };
  const s = setup(packaged({ downloader: updated }));
  s.find('resetDownloader').focus();
  s.context.nextHealth = packaged(); // back on the built-in copy: no reset button any more
  await s.find('resetDownloader').onclick();
  assert.deepEqual(s.requests.at(-1), { path: '/api/system/downloader', method: 'DELETE' });
  assert.equal(s.find('resetDownloader'), null);
  assert.equal(s.context.document.activeElement, s.find('updateDownloader'));
  // Without an update button left in the row, focus goes to Check now.
  const t = setup(packaged({ downloader: updated }));
  t.find('resetDownloader').focus();
  t.context.nextHealth = packaged({ downloader: { ...updated, source: 'bundled', update_supported: false } });
  await t.find('resetDownloader').onclick();
  assert.equal(t.find('updateDownloader'), null);
  assert.equal(t.context.document.activeElement, t.find('checkUpdates'));
});

test('downloader update keeps its pending state across health redraws and runs once', async () => {
  const s = setup(packaged());
  let finish;
  let calls = 0;
  s.context.api = () => { calls += 1; return new Promise((resolve) => { finish = resolve; }); };
  const pending = s.find('updateDownloader').onclick();
  s.render(packaged());
  assert.match(text(s.find('updateDownloader')), /Updating…/);
  await s.find('updateDownloader').onclick();
  assert.equal(calls, 1);
  finish({ ok: true, before: '2026.10.26', after: '2026.11.10', message: 'Updated to 2026.11.10.' });
  await pending;
  assert.match(text(s.find('updateDownloader')), /Update downloader/);
  assert.deepEqual(s.messages, ['Updated to 2026.11.10.']);
});

test('source install and an older packaged app keep their downloader wording', () => {
  const s = setup({ version: '2.4.0', packaged: false, ffmpeg: { ok: true }, ai: {},
    downloader: { ok: true, version: '2026.10.26', age_days: 5, update_supported: true, source: 'environment' } });
  assert.match(s.text(), /Version 2026\.10\.26 — update it if downloads start failing/);
  assert.ok(s.find('updateDownloader'));
  assert.ok(s.find('autoUpdateDownloader'));
  assert.equal(s.find('resetDownloader'), null);
  s.render({ version: '2.4.0b6', packaged: true, ffmpeg: { ok: true }, ai: {},
    downloader: { ok: true, version: '2026.08.19', age_days: 90, update_supported: false } });
  assert.match(s.text(), /Version 2026\.08\.19 · built into the app/);
  assert.match(s.text(), /Install a newer AudioLab app to update the built-in downloader/);
  assert.match(s.text(), /90 days old\. Sites like YouTube change often\./);
  assert.equal(s.find('updateDownloader'), null);
  assert.equal(s.find('autoUpdateDownloader'), null);
});

test('a health object without the new fields (or none at all) still renders', () => {
  const older = { version: '2.4.0b6', packaged: true, ffmpeg: { ok: true },
    downloader: { ok: true, version: '2026.08.19', age_days: 47, update_supported: false },
    ai: { available: false, install_supported: false, reason: 'AI pack not installed' } };
  const s = setup(older);
  assert.equal(spot(s.body, 'updates'), null, 'no update row without health.updates');
  assert.equal(s.find('checkUpdatesAuto'), null);
  assert.ok(spot(s.body, 'downloader'));
  assert.match(s.text(), /Audio engine \(ffmpeg\)/);
  for (const health of [{}, null, { updates: 'yes', downloader: null }, { updates: { app: null } }]) {
    assert.doesNotThrow(() => s.render(health));
  }
});

test('the automatic check switch shows with its setting, saves it and respects a lock', async () => {
  const s = setup(packaged(), { settings: { check_updates: false } });
  const sw = s.find('checkUpdatesAuto');
  assert.equal(sw.checked, false);
  assert.match(s.text(), /GitHub sees your IP address; nothing else is sent\./);
  await sw.onChange(true);
  assert.deepEqual(JSON.parse(JSON.stringify(s.saved)), [{ check_updates: true }]);
  const locked = setup(packaged(), { settings: { check_updates: true, locked: ['check_updates'] } });
  assert.equal(locked.find('checkUpdatesAuto').disabled, true);
  assert.equal(setup(packaged(), { settings: {} }).find('checkUpdatesAuto'), null, 'older server: no setting, no switch');
});

test('yt-dlp versions compare part by part as numbers', () => {
  const s = setup(packaged());
  assert.equal(s.run('isNewer("2026.10.26", "2026.9.1")'), true);
  assert.equal(s.run('isNewer("2026.10.26.1", "2026.10.26")'), true);
  assert.equal(s.run('isNewer("2026.10.26", "2026.10.26")'), false);
  assert.equal(s.run('isNewer("2026.09.01", "2026.10.26")'), false);
});

test('Settings groups, legacy links, focused controls and diagnostic disclosure survive redraws', () => {
  const s = setup({
    local: true, ai: { install_supported: true }, ffmpeg: { ok: true },
    updates: { app: {} }, downloader: { can_update: true }, formats: { download: [] },
  }, { settings: { cookies_browser: '', output_dir: '/example', check_updates: false } });
  s.run('body.children = []; settingsView.mount(body); renderAll()');
  assert.deepEqual([...s.run('SECTIONS.map(([, name]) => name)')],
    ['General', 'Output defaults', 'Downloads', 'AI add-on', 'Storage', 'Help']);
  const disclosure = () => s.find('settingsMaintenance');
  assert.ok(!disclosure().open);
  for (const id of ['keepWorkFiles', 'copyReport', 'downloaderSpot']) assert.ok(disclosure().contains(s.find(id)), id);
  assert.ok(s.find('settings-storage').contains(s.find('keepOriginals')));
  for (const [link, section, focus] of [
    ['output', 'defaults', 'outputDir'], ['cookies', 'downloads', 'cookiesBrowser'], ['ai', 'ai-addon', 'enableAi'],
    ['updates', 'help', 'checkUpdates'], ['downloader', 'help', 'updateDownloader'],
    ['files', 'defaults', 'outputDir'], ['look', 'general', 'settings-general'],
  ]) {
    assert.ok(s.run(`reveal("${link}", {smooth: false, flash: false})`), link);
    assert.equal(s.run(`refs.links["${section}"]["aria-current"]`), 'true', link);
    assert.equal(s.context.document.activeElement.id, focus, link);
  }
  assert.equal(disclosure().open, true);
  s.find('keepWorkFiles').focus();
  s.run('renderHelp()');
  assert.equal(disclosure().open, true);
  assert.equal(s.context.document.activeElement, s.find('keepWorkFiles'));
  s.context.store.health.local = false;
  s.run('renderDownloads()');
  assert.equal(s.find('settings-downloads').hidden, true);
  assert.equal(s.run('reveal("cookies", {})'), false);
});
