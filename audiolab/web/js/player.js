// Players on the job page: stem rows, track rows and a plain player for one file
// (comparing the original with the result is compare.js).
import { h, icon, clear, fmtDuration } from './dom.js';
import { mediaUrl, fileUrl } from './api.js';
import { callout } from './ui.js';
import { canPlay, cantPlayText } from './compare.js';

// One attempt owns its Audio and callbacks. Replacing it invalidates even A → B → A
// promises/events, while keeping each row's download available after a playback failure.
function rowPlayback(rows, iconSize) {
  let current = null;
  let audio = null;
  let attempt = 0;
  let destroyed = false;

  function setPlaying(row) {
    rows.forEach((r) => {
      const on = r === row;
      r.btn.setAttribute('aria-label', `${on ? 'Stop' : 'Play'} ${r.label}`);
      clear(r.btn).append(icon(on ? 'stop' : 'play', iconSize));
      r.el.classList.toggle('is-playing', on);
    });
  }

  function stop() {
    ++attempt;
    current = null;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio = null;
    }
    setPlaying(null);
  }

  async function toggle(row, src) {
    if (destroyed) return;
    const wasPlaying = current === row;
    stop();
    if (wasPlaying) return;
    row.notice.hidden = true;
    row.notice.textContent = '';
    const id = attempt;
    const media = new Audio();
    audio = media;
    current = row;
    media.preload = 'none';
    const active = () => !destroyed && attempt === id;
    const failed = () => {
      if (!active()) return;
      stop();
      row.notice.textContent = 'This file couldn’t be played here. Try downloading it.';
      row.notice.hidden = false;
    };
    media.addEventListener('ended', () => { if (active()) stop(); });
    media.addEventListener('error', failed);
    setPlaying(row);
    try {
      media.src = src;
      await media.play();
    } catch { failed(); }
  }

  return { toggle, destroy() { destroyed = true; stop(); } };
}

function playbackNotice() {
  return h('span', { class: 'muted small', role: 'status', 'aria-live': 'polite', hidden: true });
}

/** Stems: one row per stem with play/stop and its download. */
export function stemRows(jobId, previews, files) {
  const rows = [];
  const playback = rowPlayback(rows, 16);

  const list = h('div', { class: 'stems' });
  for (const p of previews) {
    const name = p.key.replace(/^stem:/, '');
    const label = p.label || name;
    const file = p.file_id != null
      ? (files || []).find((f) => f.id === p.file_id)
      : (files || []).find((f) => new RegExp(`(^|[ _-])${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.[a-z0-9]+$`, 'i').test(f.name));
    const btn = h('button', { type: 'button', class: 'stem-play', 'aria-label': `Play ${label}` }, icon('play', 16));
    const row = { label, btn, el: null, notice: playbackNotice() };
    btn.addEventListener('click', () => playback.toggle(row, mediaUrl(jobId, p.key)));
    row.el = h('div', { class: 'stem-row' },
      btn,
      h('div', { class: 'stem-name' }, h('strong', null, label), file ? h('span', { class: 'muted small' }, file.name) : null, row.notice),
      h('div', { class: 'stem-bars', 'aria-hidden': 'true' }, ...Array.from({ length: 5 }, () => h('i'))),
      file ? h('a', { class: 'btn btn-ghost btn-sm', href: fileUrl(jobId, file.id), download: file.name, 'aria-label': `Download ${label}` }, icon('download', 14), h('span', { class: 'dl-text' }, 'Download')) : null);
    rows.push(row);
    list.append(row.el);
  }
  return {
    el: list,
    destroy: playback.destroy,
  };
}

/** Plain player for a single result file (download jobs); a short note with the download when it can't play here. */
export function filePlayer(jobId, file) {
  const download = () => h('p', null, h('a', { href: fileUrl(jobId, file.id), download: file.name }, `Download ${file.name}`));
  if (!canPlay(file)) return callout('info', null, cantPlayText(file), download());
  const video = file.kind === 'video';
  const media = h(video ? 'video' : 'audio', {
    class: video ? 'result-video' : 'result-audio', src: mediaUrl(jobId, `file:${file.id}`),
    controls: true, preload: 'metadata', playsinline: video,
  });
  const wrap = h('div', { class: 'cmp-plain' }, media);
  media.addEventListener('error', () => {
    if (!media.error) return;
    clear(wrap).append(callout('warning', null, 'This file couldn’t be played here. It may have been moved or deleted.', download()));
  });
  return wrap;
}

/** Tracks cut from a long recording: play each inline, download each. */
export function trackRows(jobId, files) {
  const rows = [];
  const playback = rowPlayback(rows, 14);

  const list = h('ol', { class: 'tracks' });
  files.forEach((f, i) => {
    const n = f.track ?? i + 1;
    // Labels look like "01 · Artist – Track"; the number has its own column.
    const label = (f.label || f.name || '').replace(/^\d+\s*[·.\-–]\s*/, '') || `Track ${n}`;
    const length = f.start_s != null && f.end_s != null ? f.end_s - f.start_s : null;
    const btn = h('button', { type: 'button', class: 'stem-play track-play', 'aria-label': `Play ${label}` }, icon('play', 14));
    const row = { label, btn, el: null, notice: playbackNotice() };
    btn.addEventListener('click', () => playback.toggle(row, mediaUrl(jobId, `file:${f.id}`)));
    row.el = h('li', { class: 'track-row' },
      btn,
      h('span', { class: 'track-n num' }, String(n).padStart(2, '0')),
      h('div', { class: 'stem-name' }, h('strong', { title: label }, label), h('span', { class: 'muted small', title: f.name }, f.name), row.notice),
      h('span', { class: 'track-len num' }, fmtDuration(length)),
      h('a', { class: 'btn btn-ghost btn-sm', href: fileUrl(jobId, f.id), download: f.name, 'aria-label': `Download ${label}` }, icon('download', 14), h('span', { class: 'track-dl-text' }, 'Download')));
    rows.push(row);
    list.append(row.el);
  });
  return {
    el: list,
    destroy: playback.destroy,
  };
}
