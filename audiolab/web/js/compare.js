// Comparing the original with the result: one player for the Enhance job page and the
// Library, and a plain player for one Library file.
//
// The compare player plays both versions on one Web Audio clock. The server decodes them
// to 48 kHz PCM in short chunks on a shared 1/25 s grid (…/pcm/…?offset=&frames=); both
// versions' chunks start at the same AudioContext time and each chunk starts exactly where
// the one before ends. Switching crossfades two gains, so it is click-free and keeps the
// exact position; play, pause and seeks fade in and out just as briefly. Only a few seconds
// are decoded ahead: a two-hour set costs no more memory than a song, and every format plays
// (the server decodes it).
import { h, icon, clear, append, fmtDuration } from './dom.js';
import { url } from './api.js';
import { switchControl, callout, toast } from './ui.js';

const RATE = 48000;       // what the server sends
const GRID = 1920;        // frames: chunks start and end on this grid (docs/API.md)
const FIRST = 2 * RATE;   // the first chunk after play or a seek: quick to start
const CHUNK = 8 * RATE;   // then 8 s at a time
const LOW_S = 4;          // fetch the next chunk when less than this is queued
const LEAD_S = 0.05;      // the first chunk starts this far ahead of the clock
const FADE_S = 0.025;     // switching crossfade
const EDGE_S = 0.012;     // fade in at a start, out at a pause or seek: no click from cutting the wave
const STALL_MS = 1500;    // Play pressed and the clock still not running after this: say so
const TICK_MS = 200;
const SKIP_S = 10;
const SEEK_KEYS = { ArrowRight: 5, ArrowUp: 5, ArrowLeft: -5, ArrowDown: -5, PageUp: 30, PageDown: -30 }; // seconds
const SEEK_REST_MS = 150; // timeline keys: play from the new place once they rest
// Server codec names (ffprobe) → the codecs= value browsers understand (RFC 6381).
// Only containers that can hold different codecs are asked with one.
const CODECS = { opus: 'opus', vorbis: 'vorbis', flac: 'flac', aac: 'mp4a.40.2', alac: 'alac', mp3: 'mp4a.40.34' };
const MULTI_CODEC = /^(audio|video)\/(webm|ogg|mp4)$/;
const CODEC_NAMES = { opus: 'Opus', vorbis: 'Vorbis', aac: 'AAC', mp3: 'MP3', flac: 'FLAC', alac: 'ALAC' };
// A job's files carry no media type: guessed from the name (YouTube's WebM audio is Opus).
const BY_EXT = {
  mp3: ['audio/mpeg'], m4a: ['audio/mp4'], aac: ['audio/aac'], flac: ['audio/flac'], wav: ['audio/wav'],
  opus: ['audio/ogg', 'opus'], ogg: ['audio/ogg', 'vorbis'], webm: ['audio/webm', 'opus'], mp4: ['video/mp4'],
};

let uid = 0;
const quietly = (fn) => { try { Promise.resolve(fn?.()).catch(() => {}); } catch { /* already closed */ } };

/** A Library file for the players (inline, Range), or as a download. */
export const libraryFileUrl = (entryId, fileId, { download = false } = {}) =>
  url(`/api/library/${encodeURIComponent(entryId)}/files/${encodeURIComponent(fileId)}`, download ? { download: 1 } : null);

/** A codec in words; PCM says its container and bit depth ("pcm_s16le" in a WAV → "WAV 16-bit"). */
export function codecName(codec, format = '') {
  const c = String(codec || '').toLowerCase();
  if (c.startsWith('pcm_')) {
    const bits = /\d+/.exec(c)?.[0];
    return [format ? String(format).toUpperCase() : 'PCM', bits && `${bits}-bit`].filter(Boolean).join(' ');
  }
  return CODEC_NAMES[c] || c.toUpperCase();
}

/** A file's media type and codec: its own, else guessed from its name. */
function mediaOf(file) {
  if (file?.mime) return [file.mime, file.codec];
  const [mime, codec] = BY_EXT[String(file?.name || '').split('.').pop().toLowerCase()] || [];
  if (file?.kind === 'video') return [mime?.replace(/^audio\//, 'video/')]; // its codecs aren't known
  return [mime, file?.codec || codec];
}

/**
 * Whether this browser says it can play the file. A known codec in a WebM/Ogg/MP4
 * container is asked about with its codec: WebKit before macOS 14 says "maybe" to plain
 * audio/webm but can't play Opus in it. Playback errors are still handled by the players.
 */
export function canPlay(file) {
  const [mime, codecKey] = mediaOf(file);
  if (!mime) return false;
  const codec = CODECS[codecKey];
  const type = codec && MULTI_CODEC.test(mime) ? `${mime}; codecs="${codec}"` : mime;
  return document.createElement('audio').canPlayType(type) !== '';
}

/** Why a file can't play here, in one line (Opus and Vorbis need macOS 14 in the Mac app's window). */
export function cantPlayText(file) {
  const codec = mediaOf(file)[1];
  const what = file?.label || codecName(codec) || (file?.format || '').toUpperCase() || 'these';
  return ['opus', 'vorbis'].includes(codec) && /Mac/i.test(navigator.platform || navigator.userAgent)
    ? `This Mac can’t play ${codecName(codec)} files here — that needs macOS 14 or newer. Download it to listen.`
    : `This browser can’t play ${what} files here. Download it to listen.`;
}

/** "Enhanced · WAV 16-bit": what a compared result is, from its format label. */
const resultLabel = (file) =>
  `Enhanced · ${String(file?.label || file?.format || '').replace(/ \(\.[a-z0-9]+\)$/i, '').replace(/ · /g, ' ')}`.replace(/ · $/, '');

/** The full-length comparison of a Library entry: its kept original and its `compare_file_id`. */
export function libraryCompare(entry, extra = {}) {
  const file = (entry.files || []).find((f) => f.id === entry.compare_file_id);
  const side = (fileId, label) => ({
    label,
    url: (offset, frames) => url(`/api/library/${encodeURIComponent(entry.id)}/pcm/${encodeURIComponent(fileId)}`, { offset, frames }),
    download: libraryFileUrl(entry.id, fileId, { download: true }),
  });
  const duration = entry.duration_s || entry.source?.duration_s;
  return comparePlayer({
    sides: [side('original', 'Original'), side(file.id, resultLabel(file))],
    length: duration,
    songDuration: duration,
    markers: entry.markers,
    loudness: entry.loudness,
    ...extra,
  });
}

/**
 * Original | result, sample-locked.
 *
 * opts.sides: [{ label, url(offset, frames), download? }, …] — the original first.
 * songOffset: the song time of the sides' frame 0 (an excerpt's start), length: how long
 * the sides are, songDuration: the whole song (for "1:52 / 4:45"), startAt: the song time to
 * start from, markers: tracks/chapters in song time, loudness: {original_lufs, result_lufs}
 * of the whole song for "Match volume" (null: no switch), note: one line under the player.
 */
export function comparePlayer(opts) {
  const sides = opts.sides.map((s) => ({ ...s, gain: null }));
  const songOffset = opts.songOffset || 0;
  let lengthF = Math.max(GRID, Math.round((Number(opts.length) || 0) * RATE)); // grows/shrinks to the real end
  const songDuration = () => opts.songDuration || songOffset + lengthF / RATE;
  const loud = opts.loudness || {};
  const hasLoudness = Number.isFinite(loud.original_lufs) && Number.isFinite(loud.result_lufs);
  const markers = (opts.markers || []).filter((m) => Number.isFinite(m.start_s)
    && m.start_s >= songOffset && m.start_s < songOffset + lengthF / RATE);
  const id = `cmp-${++uid}`;
  const label = sides[1].label;
  const AC = window.AudioContext || window.webkitAudioContext;

  let ctx = null;
  let master = null;    // after both versions: the fades at play, pause and seeks (the sides' gains do A/B)
  let active = 1;
  let playing = false;
  let matchVolume = true;
  let position = clamp(((opts.startAt ?? songOffset) - songOffset) * RATE); // frames, while stopped
  let gen = 0;          // one per play or seek: answers for an older one are dropped
  let abort = null;     // the running fetches
  let queue = [];       // scheduled chunks, oldest first: { frame, frames, when, nodes }
  let anchor = null;    // { frame, when }: `frame` plays at context time `when`
  let nextFrame = 0;    // where the next chunk starts
  let fetching = false;
  let endFrame = null;  // set once the last chunk is scheduled
  let timer = 0;
  let dragging = false;
  let markerIndex = -1;
  let dead = false;
  let stall = 0;        // the check after Play that the clock runs
  let stalled = false;  // … and it didn't: the next Play starts with a new context
  let soon = 0;         // a seek from the timeline's keys, waiting for them to rest

  const root = h('div', { class: 'ab cmp', tabindex: '-1', role: 'group', 'aria-label': `Compare the original with ${label}` });
  if (!AC) {
    root.append(callout('info', null, 'This browser can’t play the comparison.', downloads()));
    return { el: root, pause() {}, destroy() {} };
  }

  const playBtn = h('button', { type: 'button', class: 'ab-play', 'aria-label': 'Play' }, icon('play', 22));
  const seek = h('input', { type: 'range', class: 'ab-seek', min: 0, max: 1, step: 'any', value: 0, 'aria-label': 'Position' }); // seconds
  const ticks = h('div', { class: 'cmp-ticks', 'aria-hidden': 'true' });
  const time = h('span', { class: 'ab-time' });
  const keys = h('div', { class: 'ab-keys', role: 'radiogroup', 'aria-label': 'Version' });
  const volumeNote = h('span', { class: 'cmp-volume-note' });
  const problem = h('div', { class: 'cmp-problem', role: 'alert', hidden: true });
  const markerList = markers.length > 1 ? h('div', { class: 'cmp-markers', role: 'group', 'aria-label': 'Jump to' }) : null;

  function clamp(f) {
    return Math.max(0, Math.min(Math.round(f) || 0, lengthF));
  }

  function downloads() {
    const links = sides.filter((s) => s.download)
      .map((s) => h('a', { href: s.download, download: true }, s === sides[0] ? 'Download the original' : 'Download the result'));
    return links.length ? h('p', null, ...links.flatMap((a, i) => (i ? [' · ', a] : [a]))) : null;
  }

  // ---------------------------------------------------------------- A/B switch
  function renderKeys() {
    clear(keys);
    sides.forEach((s, i) => {
      const on = i === active;
      const b = h('button', {
        type: 'button', class: 'ab-key', role: 'radio', 'aria-checked': String(on), tabindex: on ? '0' : '-1',
        'aria-keyshortcuts': String(i + 1),
      }, h('span', { class: 'ab-key-label' }, s.label), h('kbd', { 'aria-hidden': 'true' }, String(i + 1)));
      b.addEventListener('click', () => select(i));
      keys.append(b);
    });
  }

  keys.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
    e.preventDefault();
    select(1 - active);
    keys.children[active]?.focus();
  });

  function select(i) {
    if (i === active || !sides[i]) return;
    active = i;
    applyGains(true);
    const focused = keys.contains(document.activeElement);
    renderKeys();
    if (focused) keys.children[active].focus();
  }

  // ---------------------------------------------------------------- volume
  /** Each side's gain: audible or not × the loudness match (the louder side is turned down). */
  function levels() {
    const diff = hasLoudness ? loud.result_lufs - loud.original_lufs : 0; // > 0: the result is louder
    const down = matchVolume ? 10 ** (-Math.abs(diff) / 20) : 1;
    return sides.map((s, i) => (i === active ? 1 : 0) * ((i === 1 ? diff > 0 : diff < 0) ? down : 1));
  }

  function applyGains(fade = false) {
    if (!ctx) return;
    const t = ctx.currentTime;
    levels().forEach((v, i) => {
      const g = sides[i].gain.gain;
      g.cancelScheduledValues(t);
      if (fade && playing) g.setTargetAtTime(v, t, FADE_S / 3); // both together: a crossfade
      else g.setValueAtTime(v, t);
    });
  }

  function renderVolume() {
    if (!hasLoudness) return;
    const diff = loud.result_lufs - loud.original_lufs;
    const louder = diff > 0 ? label.split(' · ')[0] : 'Original';
    const db = Math.abs(diff);
    if (db < 0.1) volumeNote.textContent = 'Both are equally loud.';
    else if (matchVolume) volumeNote.textContent = `${louder} is ${db.toFixed(1)} dB louder, turned down to match.`;
    else volumeNote.textContent = `${louder} is ${db.toFixed(1)} dB louder.`;
  }

  // ---------------------------------------------------------------- engine
  /** The AudioContext, made on the first Play (WebKit lets only a user gesture start sound). */
  function context() {
    if (ctx) return ctx;
    try { ctx = new AC({ sampleRate: RATE }); } catch { ctx = new AC(); } // else every chunk is resampled: fine
    stalled = false;
    master = ctx.createGain();
    master.connect(ctx.destination);
    sides.forEach((s) => { s.gain = ctx.createGain(); s.gain.connect(master); });
    applyGains();
    return ctx;
  }

  /** Let go of the context (it is made again on the next Play). */
  function dropContext() {
    halt();
    const c = ctx;
    quietly(() => c?.close?.());
    ctx = null;
    master = null;
    sides.forEach((s) => { s.gain = null; });
  }

  /** The frame playing now. */
  function now() {
    if (!playing || !anchor) return position;
    const t = ctx.currentTime;
    let f = anchor.frame;
    for (const c of queue) {
      if (t < c.when) break;
      f = c.frame + Math.min(c.frames, Math.round((t - c.when) * RATE)); // in a gap (late chunk): its end
    }
    return clamp(f);
  }

  /** Stop the scheduled chunks — `fade`: after a short fade-out of both versions, not mid-wave. */
  function stopSources(fade) {
    const at = fade && ctx ? ctx.currentTime + EDGE_S : 0;
    if (at) {
      const g = master.gain;
      g.cancelScheduledValues(ctx.currentTime);
      g.setValueAtTime(g.value, ctx.currentTime);
      g.linearRampToValueAtTime(0, at);
    }
    for (const c of queue) {
      for (const n of c.nodes) {
        if (!n) continue;
        try { n.stop(at); } catch { /* not started yet */ }
        if (!at) n.disconnect();
      }
    }
    queue = [];
  }

  /** Stop what is scheduled and fetching; the next play or seek starts afresh. */
  function halt(fade = false) {
    gen++;
    abort?.abort();
    abort = null;
    stopSources(fade);
    anchor = null;
    fetching = false;
    clearInterval(timer);
    timer = 0;
    clearTimeout(soon);
  }

  /** Fade in from silence at `when` (a start, a seek, or after a late chunk). */
  function fadeIn(when) {
    const g = master.gain;
    g.cancelScheduledValues(when);
    g.setValueAtTime(0, when);
    g.linearRampToValueAtTime(1, when + EDGE_S);
  }

  /** Play from `frame` (snapped to the grid): the first chunk of both, then the rest as it goes. */
  function startAt(frame) {
    halt(true);
    position = Math.floor(clamp(frame) / GRID) * GRID;
    nextFrame = position;
    endFrame = null;
    abort = new AbortController();
    timer = setInterval(tick, TICK_MS);
    fill(gen, FIRST);
  }

  async function load(side, offset, frames, signal) {
    const res = await fetch(side.url(offset, frames), { signal });
    if (!res.ok) {
      const detail = await res.json().then((d) => (typeof d?.detail === 'string' ? d.detail : ''), () => '');
      throw Object.assign(new Error(detail || `error ${res.status}`), { status: res.status, side, detail });
    }
    const data = new Float32Array(await res.arrayBuffer());
    const n = data.length >> 1;
    if (!n) return null;
    const buf = ctx.createBuffer(2, n, RATE);
    const l = buf.getChannelData(0);
    const r = buf.getChannelData(1);
    for (let i = 0, j = 0; i < n; i++, j += 2) { l[i] = data[j]; r[i] = data[j + 1]; }
    return buf;
  }

  async function fill(my, frames) {
    if (fetching || endFrame != null) return;
    fetching = true;
    const at = nextFrame;
    let bufs;
    try {
      bufs = await Promise.all(sides.map((s) => load(s, at, frames, abort.signal)));
    } catch (e) {
      if (my !== gen || e.name === 'AbortError') return;
      fail(e);
      return;
    }
    if (my !== gen) return;
    fetching = false;
    const n = Math.max(0, ...bufs.map((b) => b?.length || 0));
    if (n < frames) { // the last chunk (both are shorter than asked)
      endFrame = at + n;
      lengthF = endFrame;
      renderLength();
    } else if (at + n > lengthF) { // longer than the length we were told (or it wasn't known)
      lengthF = at + n;
      renderLength();
    }
    if (n) {
      // Each chunk starts where the one before ends, on the context's clock. A late one (the
      // fetch took longer than what was queued) starts a little later instead: a short gap.
      let when = anchor ? anchor.when + (at - anchor.frame) / RATE : 0;
      if (!anchor || when < ctx.currentTime + 0.01) {
        when = Math.ceil((ctx.currentTime + LEAD_S) * ctx.sampleRate) / ctx.sampleRate;
        anchor = { frame: at, when };
        fadeIn(when);
      }
      const nodes = bufs.map((b, i) => {
        if (!b) return null;
        const src = ctx.createBufferSource();
        src.buffer = b;
        src.connect(sides[i].gain);
        src.start(when); // the same time for both versions
        return src;
      });
      queue.push({ frame: at, frames: n, when, nodes });
      nextFrame = at + n;
    }
    tick();
  }

  function tick() {
    if (!playing) return;
    const t = ctx.currentTime;
    while (queue.length > 1 && queue[0].when + queue[0].frames / RATE < t - 0.5) queue.shift(); // played
    const f = now();
    if (endFrame != null && f >= endFrame) {
      pause();
      position = lengthF;
      renderTime();
      return;
    }
    const queued = anchor ? anchor.when + (nextFrame - anchor.frame) / RATE - t : 0;
    if (anchor && queued < LOW_S) fill(gen, CHUNK);
    renderTime(f);
  }

  /** A file that is gone ends the player; anything else (busy, a hiccup) stops it with Try again. */
  function fail(e) {
    const which = e.side === sides[0] ? 'original' : 'result';
    if (e.status === 404 || e.status === 410) {
      destroy();
      clear(root).append(callout('warning', null, `The ${which} couldn’t be loaded. It may have been moved or deleted.`, downloads()));
      return;
    }
    pause();
    const why = e.detail || (e.status ? `error ${e.status}` : 'the app can’t be reached');
    const again = h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: () => play() }, icon('retry', 14), 'Try again');
    clear(problem).append(callout('warning', null, `The ${which} couldn’t be played (${why.replace(/\.$/, '')}).`,
      h('div', { class: 'callout-actions' }, again)));
    problem.hidden = false;
  }

  // ---------------------------------------------------------------- transport
  function play() {
    if (dead) return;
    if (problem.contains(document.activeElement)) playBtn.focus();
    problem.hidden = true;
    // A context the system closed or interrupted (sleep, another app took the output), or one
    // that didn't start last time: start over with a new one, while inside this click.
    if (ctx && (stalled || ctx.state === 'closed' || ctx.state === 'interrupted')) dropContext();
    context();
    quietly(() => ctx.resume?.()); // inside the click: a suspended or new context may start now
    if (position >= lengthF) position = 0; // played to the end: from the start
    playing = true;
    renderPlay();
    startAt(position);
    // The clock still not running a moment later (the output is busy, the system stopped it):
    // don't sit there silently showing Pause.
    clearTimeout(stall);
    stall = setTimeout(() => {
      if (!playing || ctx.state === 'running') return;
      pause();
      stalled = true;
      toast('The sound didn’t start. Press Play to try again.', { kind: 'warning' });
    }, STALL_MS);
  }

  /** Stop and keep the position (after a short fade-out). */
  function stop() {
    if (!playing) return;
    position = now();
    playing = false;
    clearTimeout(stall);
    halt(true);
    renderPlay();
    renderTime();
  }

  function pause() {
    if (!playing) return;
    stop();
    // Once the fade-out has played, let the output rest — unless Play came first.
    const c = ctx;
    const my = gen;
    setTimeout(() => { if (!playing && my === gen && ctx === c) quietly(() => c.suspend?.()); }, EDGE_S * 1000 + 50);
  }

  /** To `s` seconds of the sides; playing carries on from there. */
  function seekTo(s) {
    const f = clamp(Math.min(s * RATE, lengthF - GRID));
    if (playing) startAt(f);
    else position = f;
    renderTime(f);
  }

  /** A seek from the timeline's keys: the position moves at once, the sound follows once the
   *  keys rest, so holding an arrow key doesn't load every step on the way. */
  function seekSoon(s) {
    if (!playing) { seekTo(s); return; }
    halt(true);
    position = clamp(Math.min(s * RATE, lengthF - GRID));
    renderTime(position);
    soon = setTimeout(() => { if (playing) startAt(position); }, SEEK_REST_MS);
  }

  function renderPlay() {
    clear(playBtn).append(icon(playing ? 'pause' : 'play', 22));
    playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }

  function renderTime(f = now()) {
    const t = songOffset + f / RATE;
    renderMarker(t);
    if (dragging) return; // the timeline and time show where it's dragged to
    seek.value = f / RATE;
    renderFill();
    seek.setAttribute('aria-valuetext', `${fmtDuration(t)} of ${fmtDuration(songDuration())}`);
    time.textContent = `${fmtDuration(t)} / ${fmtDuration(songDuration())}`;
  }

  function renderFill() {
    seek.style.setProperty('--p', `${(seek.value / seek.max) * 100}%`);
  }

  /** While dragging: the time and fill of where the timeline is. */
  function renderDrag() {
    const t = songOffset + Number(seek.value);
    renderFill();
    seek.setAttribute('aria-valuetext', `${fmtDuration(t)} of ${fmtDuration(songDuration())}`);
    time.textContent = `${fmtDuration(t)} / ${fmtDuration(songDuration())}`;
  }

  /** The timeline's scale and the marker ticks, after the length changed. */
  function renderLength() {
    seek.max = lengthF / RATE; // a value past the new end moves back to it
    renderTicks();
    if (dragging) renderDrag();
  }

  // ---------------------------------------------------------------- markers
  function renderTicks() {
    clear(ticks);
    for (const m of markers) {
      const x = (m.start_s - songOffset) * RATE / lengthF;
      if (x > 0 && x < 1) ticks.append(h('i', { style: { left: `${x * 100}%` } }));
    }
  }

  function renderMarker(t) {
    if (!markerList) return;
    let i = -1;
    markers.forEach((m, j) => { if (m.start_s <= t + 0.05) i = j; });
    if (i === markerIndex) return;
    markerIndex = i;
    [...markerList.children].forEach((b, j) => {
      if (j === i) b.setAttribute('aria-current', 'true');
      else b.removeAttribute('aria-current');
    });
  }

  if (markerList) {
    markers.forEach((m, i) => {
      const title = m.title || `Track ${m.n ?? i + 1}`;
      markerList.append(h('button', {
        type: 'button', class: 'filter cmp-marker', title, 'aria-label': `${title}, at ${fmtDuration(m.start_s)}`,
        onclick: () => seekTo(m.start_s - songOffset),
      }, h('span', { class: 'num cmp-marker-time' }, fmtDuration(m.start_s)), h('span', { class: 'cmp-marker-title' }, title)));
    });
  }

  // ---------------------------------------------------------------- events
  playBtn.addEventListener('click', () => (playing ? pause() : play()));
  seek.addEventListener('input', () => {
    dragging = true;
    renderDrag();
  });
  seek.addEventListener('change', () => {
    dragging = false;
    seekTo(Number(seek.value));
  });
  // Arrow keys move 5 s, Page Up/Down 30 s, Home/End to the start/end.
  seek.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const end = lengthF / RATE;
    const to = e.key in SEEK_KEYS ? Number(seek.value) + SEEK_KEYS[e.key] : { Home: 0, End: end }[e.key];
    if (to == null) return;
    e.preventDefault();
    seekSoon(Math.max(0, Math.min(to, end)));
  });

  // 1/2 from anywhere in the player; Space plays/pauses unless it belongs to the focused
  // control (a button, the Match volume switch). The checked version key and the timeline
  // have nothing of their own to do with Space.
  root.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '1' || e.key === '2') { select(Number(e.key) - 1); e.preventDefault(); }
    if (e.key === ' ' && (e.target === root || e.target === seek || keys.contains(e.target))) {
      e.preventDefault();
      if (!e.repeat) playing ? pause() : play(); // a held key: once
    }
  });

  const skip = (s, text) => h('button', {
    type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', 'aria-label': text,
    onclick: () => seekTo(now() / RATE + s),
  }, s < 0 ? `−${-s} s` : `+${s} s`);

  const matchSwitch = hasLoudness ? switchControl({
    id: `${id}-match`, checked: true, label: 'Match volume',
    onChange: (v) => { matchVolume = v; applyGains(true); renderVolume(); },
  }) : null;

  // Version tabs; play, timeline and time; the note on where it starts; −10/+10 s and Match
  // volume with its one line; the track and chapter starts.
  append(root, [
    keys,
    problem,
    h('div', { class: 'ab-transport' }, playBtn, h('div', { class: 'cmp-timeline' }, seek, ticks), time),
    opts.note ? h('p', { class: 'cmp-note' }, opts.note) : null,
    h('div', { class: 'cmp-tools' },
      h('div', { class: 'cmp-skip' }, skip(-SKIP_S, `Back ${SKIP_S} seconds`), skip(SKIP_S, `Forward ${SKIP_S} seconds`)),
      matchSwitch ? h('label', { class: 'details-toggle', for: `${id}-match` }, matchSwitch, 'Match volume') : null,
      hasLoudness ? h('p', { class: 'ab-matched' }, volumeNote) : null),
    markerList]);
  renderKeys();
  renderPlay();
  renderVolume();
  renderLength();
  renderTime();

  function destroy() {
    if (dead) return;
    dead = true;
    playing = false;
    clearTimeout(stall);
    halt();
    const c = ctx;
    quietly(() => c?.close?.());
    ctx = null;
  }

  /** Small transport state for another full-length comparison of the same recording. */
  function snapshot() {
    if (dead) return null;
    const frame = now();
    return { time: songOffset + frame / RATE, playing: playing && frame < lengthF,
      side: active === 0 ? 'original' : 'result', matchVolume };
  }

  /** Restore while paused; resuming requires the caller to still be inside a user gesture. */
  function restore(state, { resume = false } = {}) {
    if (dead || !state || !Number.isFinite(state.time)) return;
    pause();
    position = clamp((state.time - songOffset) * RATE);
    active = state.side === 'original' ? 0 : 1;
    if (typeof state.matchVolume === 'boolean') matchVolume = state.matchVolume;
    if (matchSwitch) matchSwitch.checked = matchVolume;
    applyGains();
    renderKeys();
    renderVolume();
    renderTime();
    // A shorter version may already be at its end. Keep it there, rather than replaying
    // from zero (the usual behavior of an explicit Play at the end).
    if (resume && state.playing && position < lengthF) play();
  }

  return { el: root, pause, destroy, snapshot, restore };
}

/** A plain player for one file of a Library entry (with the browser's own controls). */
export function plainPlayer(entry, file) {
  const wrap = h('div', { class: 'cmp-plain' });
  if (!canPlay(file)) {
    wrap.append(callout('info', null, cantPlayText(file)));
    return { el: wrap, play() {}, pause() {}, destroy() {} };
  }
  const video = (file.mime || '').startsWith('video/');
  const media = h(video ? 'video' : 'audio', {
    class: video ? 'result-video' : 'result-audio', src: libraryFileUrl(entry.id, file.id),
    controls: true, preload: 'metadata', playsinline: video, 'aria-label': file.label || file.name,
  });
  media.addEventListener('error', () => {
    if (!media.error) return;
    clear(wrap).append(callout('warning', null, 'This file couldn’t be played. It may have been moved or deleted.'));
  });
  wrap.append(media);
  return {
    el: wrap,
    play: () => media.play().catch(() => {}),
    pause: () => media.pause(),
    destroy() { media.pause(); media.removeAttribute('src'); media.load(); },
  };
}
