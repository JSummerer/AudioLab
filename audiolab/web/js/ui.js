// Shared UI pieces: toasts, dialogs (copy text, access token), form controls, info cards,
// content blocks, callouts, meters and LEDs.
import { h, icon, clear, append } from './dom.js';
import { setToken, getToken } from './api.js';

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------
export function toast(message, { kind = 'info', action = null, timeout = 4500 } = {}) {
  const root = document.getElementById('toasts');
  const el = h('div', { class: ['toast', `toast-${kind}`] },
    h('span', { class: 'toast-msg' }, message),
    action ? h('button', {
      type: 'button', class: 'toast-action',
      onclick: () => { action.run(); dismiss(); },
    }, action.label) : null,
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss', onclick: () => dismiss() }, icon('close', 14)),
  );
  root.append(el);
  requestAnimationFrame(() => el.classList.add('is-in'));
  let timer = setTimeout(dismiss, timeout);
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', () => { timer = setTimeout(dismiss, 2000); });
  function dismiss() {
    clearTimeout(timer);
    el.classList.remove('is-in');
    setTimeout(() => el.remove(), 200);
  }
  return dismiss;
}

/** A failed request as a toast: the server's message, then its first hint (ApiError.hints). */
export function toastError(e, { action = null, timeout = 0 } = {}) {
  const hint = e?.hints?.[0];
  return toast(hint ? [e.message, h('br'), hint] : e?.message || 'Something went wrong.',
    { kind: 'danger', action, timeout: timeout || (hint ? 9000 : 6000) });
}

// ---------------------------------------------------------------------------
// Clipboard. `source` is text or a function returning a Promise of text. The
// promise goes straight into a ClipboardItem where supported, so the click's
// permission survives the network round trip (Safari). Plain-http servers on
// the network have no Clipboard API: fall back to execCommand, and finally to
// a dialog that shows the text for copying by hand.
// ---------------------------------------------------------------------------
export async function copyText(source, { done = 'Copied', title = 'Copy this text' } = {}) {
  const load = typeof source === 'function' ? source : () => Promise.resolve(source);
  let pending = null;
  const text = () => (pending ||= load());
  const cb = window.isSecureContext ? navigator.clipboard : null;
  try {
    if (cb?.write && typeof ClipboardItem !== 'undefined') {
      await cb.write([new ClipboardItem({ 'text/plain': text().then((t) => new Blob([t], { type: 'text/plain' })) })]);
      toast(done, { kind: 'ok', timeout: 2000 });
      return true;
    }
  } catch (e) {
    if (pending) { try { await pending; } catch (err) { toast(err.message, { kind: 'danger' }); return false; } }
  }
  let value;
  try { value = await text(); } catch (e) { toast(e.message, { kind: 'danger' }); return false; }
  try {
    if (cb?.writeText) {
      await cb.writeText(value);
      toast(done, { kind: 'ok', timeout: 2000 });
      return true;
    }
  } catch { /* fall through */ }
  if (legacyCopy(value)) {
    toast(done, { kind: 'ok', timeout: 2000 });
    return true;
  }
  textDialog(title, value);
  return false;
}

function legacyCopy(value) {
  const ta = h('textarea', { readonly: true, style: { position: 'fixed', top: '-1000px', opacity: '0' } });
  ta.value = value;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

function textDialog(title, value) {
  closeInfo();
  const ta = h('textarea', { class: 'field text-dump mono', readonly: true, rows: 12, 'aria-label': title });
  ta.value = value;
  const dlg = h('dialog', { class: 'dialog dialog-wide' },
    h('form', { method: 'dialog', class: 'dialog-form' },
      h('h2', null, title),
      h('p', { class: 'muted' }, 'Copying isn’t allowed here. Select the text below and copy it with ⌘C or Ctrl+C.'),
      ta,
      h('div', { class: 'dialog-actions' }, h('button', { type: 'submit', class: 'btn btn-key' }, 'Close'))));
  document.body.append(dlg);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.showModal();
  ta.focus();
  ta.select();
}

// ---------------------------------------------------------------------------
// Access-token dialog (only when the server requires one)
// ---------------------------------------------------------------------------
let tokenDialogOpen = false;

export function tokenDialog(onSaved) {
  if (tokenDialogOpen) return;
  tokenDialogOpen = true;
  closeInfo();
  const input = h('input', { type: 'password', class: 'field', id: 'tokenInput', autocomplete: 'off', value: getToken() || null, required: true });
  const dlg = h('dialog', { class: 'dialog' },
    h('form', { method: 'dialog', class: 'dialog-form' },
      h('div', { class: 'dialog-icon' }, icon('key', 22)),
      h('h2', null, 'Access token needed'),
      h('p', { class: 'muted' }, 'This server is shared on the network and asks for a token. You’ll find it in the server’s startup log.'),
      h('label', { for: 'tokenInput', class: 'field-label' }, 'Token'),
      input,
      h('div', { class: 'dialog-actions' },
        h('button', { type: 'submit', class: 'btn btn-primary', value: 'ok' }, 'Unlock'))));
  document.body.append(dlg);
  dlg.addEventListener('close', () => {
    tokenDialogOpen = false;
    const value = input.value.trim();
    dlg.remove();
    if (dlg.returnValue === 'ok' && value) {
      setToken(value);
      onSaved?.();
    }
  });
  dlg.showModal();
  input.focus();
}

// ---------------------------------------------------------------------------
// Form controls
// ---------------------------------------------------------------------------
let uid = 0;
const nextId = (p) => `${p}-${++uid}`;

/** Yes/no question in a modal dialog (Cancel has the focus); resolves true for the confirm button. */
export function confirmDialog({ title, message, confirm }) {
  closeInfo(); // its Esc listener would take the dialog's first Esc
  return new Promise((resolve) => {
    const titleId = nextId('confirm');
    const dlg = h('dialog', { class: 'dialog', 'aria-labelledby': titleId },
      h('form', { method: 'dialog', class: 'dialog-form' },
        h('h2', { id: titleId }, title),
        h('p', { class: 'muted' }, message),
        h('div', { class: 'dialog-actions' },
          h('button', { type: 'submit', class: 'btn btn-ghost', value: 'cancel' }, 'Cancel'),
          h('button', { type: 'submit', class: 'btn btn-primary', value: 'ok' }, confirm))));
    document.body.append(dlg);
    dlg.addEventListener('close', () => {
      dlg.remove();
      resolve(dlg.returnValue === 'ok');
    });
    dlg.showModal();
  });
}

/**
 * Single choice as one row of pills (radio inputs underneath, so arrow keys work).
 * options: [{ value, label, desc?, disabled?, reason?, info? }] — info: see infoButton().
 * When options have a `desc` or `info`, the pills come in a `.seg-wrap` with one line under
 * them: the picked option's description and one (i) for it. `wrap.sync()` re-reads the pick
 * (after inputs were unchecked from code); `querySelectorAll('input')` reaches the radios
 * either way. A disabled option shows its `reason` after its label.
 */
export function segmented({ label, options, value, onChange, size = 'md', name = 'seg' }) {
  const group = h('div', {
    class: ['seg', `seg-${size}`], role: 'radiogroup', 'aria-label': label || null, dataset: { n: String(options.length) },
  });
  const groupName = nextId(name);
  const noted = options.some((o) => o.desc || o.info);
  let sync = () => {};
  for (const o of options) {
    const id = nextId('opt');
    const input = h('input', {
      type: 'radio', name: groupName, id, value: String(o.value),
      checked: String(o.value) === String(value), disabled: !!o.disabled,
    });
    input.addEventListener('change', () => {
      if (!input.checked) return;
      onChange(o.value);
      sync();
    });
    group.append(h('label', { class: ['seg-opt', o.disabled && 'is-disabled'], for: id, title: (o.disabled && o.reason) || o.desc || null },
      input,
      h('span', { class: 'seg-label' }, o.label),
      o.disabled && o.reason ? h('span', { class: 'seg-reason' }, o.reason) : null));
  }
  if (!noted) return group;
  let picked = null;
  const text = h('span', { class: 'seg-note-text' });
  const [btn, card] = infoButton('About', () => picked?.info?.() || {});
  const note = h('div', { class: 'seg-note' }, text, btn, card);
  const wrap = h('div', { class: 'seg-wrap' }, group, note);
  sync = () => {
    const v = group.querySelector('input:checked')?.value;
    const next = options.find((o) => String(o.value) === v) || null;
    if (next !== picked && openInfo?.btn === btn) closeInfo();
    picked = next;
    text.textContent = picked?.desc || '';
    btn.hidden = !picked?.info;
    btn.setAttribute('aria-label', picked ? `About ${picked.label}` : 'About');
    note.hidden = !picked || (!picked.desc && !picked.info);
  };
  wrap.sync = sync;
  sync();
  return wrap;
}

/**
 * Multiple choice as a checklist (two columns when there is room). values: Set
 * Each row: the box and label, then its (i); the description under them is a second label of
 * the same box, so clicking it toggles too and the (i) stays right after the label.
 */
export function chips({ label, options, values, onChange, size = 'md' }) {
  const group = h('div', { class: ['seg', 'seg-chips', `seg-${size}`], role: 'group', 'aria-label': label || null });
  const current = new Set(values);
  for (const o of options) {
    const id = nextId('chip');
    const input = h('input', { type: 'checkbox', id, value: o.value, checked: values.has(o.value), disabled: !!o.disabled });
    input.addEventListener('change', () => {
      if (input.checked) current.add(o.value); else current.delete(o.value);
      onChange(new Set(current));
    });
    group.append(h('div', { class: ['seg-item', o.disabled && 'is-disabled'] },
      h('label', { class: ['seg-opt', 'chip', o.disabled && 'is-disabled'], for: id },
        input,
        h('span', { class: 'chip-box', 'aria-hidden': 'true' }, icon('check', 12)),
        h('span', { class: 'seg-label' }, o.label)),
      ...(o.info ? infoButton(`About ${o.label}`, o.info) : []),
      o.desc ? h('label', { class: 'seg-desc', for: id }, o.desc) : null));
  }
  return group;
}

// ---------------------------------------------------------------------------
// Info cards: an (i) button that opens a small card right after it — on click,
// Enter or Space, never on hover. One card is open at a time; Esc (focus goes
// back to the button) or a click outside closes it. `content()` builds the card
// each time it opens, so it can depend on current choices: { title, lines, note }.
// ---------------------------------------------------------------------------
let openInfo = null; // { btn, card }

export function infoButton(label, content) {
  const card = h('div', { class: 'info-card', id: nextId('info'), hidden: true });
  const btn = h('button', {
    type: 'button', class: 'info-btn', 'aria-label': label, 'aria-expanded': 'false', 'aria-controls': card.id,
  }, icon('info', 16));
  btn.addEventListener('click', () => { if (card.hidden) showInfo(btn, card, content()); else closeInfo(); });
  return [btn, card];
}

function showInfo(btn, card, { title, lines = [], note = null } = {}) {
  closeInfo();
  append(clear(card), [
    title ? h('strong', { class: 'info-card-title' }, title) : null,
    lines.length ? h('ul', { class: 'info-card-lines' }, lines.map((t) => h('li', null, t))) : h('p', { class: 'muted' }, 'No details yet.'),
    note ? h('p', { class: 'info-card-note' }, note) : null]);
  card.style.removeProperty('--shift');
  card.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  openInfo = { btn, card };
  // Stay on screen: pull the card left when it would stick out on the right.
  const r = card.getBoundingClientRect();
  const edge = document.documentElement.clientWidth - 12;
  if (r.right > edge) card.style.setProperty('--shift', `${-Math.max(0, Math.min(r.right - edge, r.left - 12))}px`);
  card.scrollIntoView({ block: 'nearest' });
  document.addEventListener('keydown', onInfoKey, true);
  document.addEventListener('pointerdown', onInfoPointer, true);
}

export function closeInfo({ refocus = false } = {}) {
  if (!openInfo) return;
  const { btn, card } = openInfo;
  openInfo = null;
  card.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('keydown', onInfoKey, true);
  document.removeEventListener('pointerdown', onInfoPointer, true);
  if (refocus && btn.isConnected) btn.focus();
}

function onInfoKey(e) {
  if (e.key !== 'Escape') return;
  // Its view was redrawn meanwhile: just forget it, leave the key to others.
  if (!openInfo.btn.isConnected) { closeInfo(); return; }
  e.preventDefault();
  e.stopPropagation();
  closeInfo({ refocus: true });
}

function onInfoPointer(e) {
  // A click on the button itself toggles it (its click handler closes the card).
  if (openInfo && !openInfo.card.contains(e.target) && !openInfo.btn.contains(e.target)) closeInfo();
}

export function switchControl({ id, checked, onChange, label, disabled }) {
  const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch', id: id || nextId('sw'), checked: !!checked, disabled: !!disabled, 'aria-label': label || null });
  input.addEventListener('change', () => onChange(input.checked));
  return input;
}

/** Number slider with an "Auto" checkbox (the slider shows once Auto is off). value null = auto. */
export function autoRange({ min, max, step, value, unit, onChange, autoLabel = 'Auto', fallback }) {
  const isAuto = value == null;
  const start = isAuto ? (fallback ?? (min + max) / 2) : value;
  const out = h('output', { class: 'range-out' });
  const range = h('input', { type: 'range', min, max, step, value: start, disabled: isAuto, 'aria-label': 'Value' });
  const autoId = nextId('auto');
  const auto = h('input', { type: 'checkbox', id: autoId, checked: isAuto });
  // On Auto only the checkbox shows: the slider and its value appear once it is off.
  const show = () => {
    out.textContent = `${Number(range.value).toFixed(step < 1 ? 1 : 0).replace('-', '−')} ${unit}`;
    range.hidden = auto.checked;
    out.hidden = auto.checked;
  };
  range.addEventListener('input', () => { show(); onChange(Number(range.value)); });
  auto.addEventListener('change', () => {
    range.disabled = auto.checked;
    show();
    onChange(auto.checked ? null : Number(range.value));
  });
  show();
  return h('div', { class: 'auto-range' },
    h('label', { class: 'auto-toggle', for: autoId }, auto, autoLabel),
    range, out);
}

// ---------------------------------------------------------------------------
// Progress bars and status lights
// ---------------------------------------------------------------------------
export function meter(pct, { idle = false, size = 'sm', label = 'Progress' } = {}) {
  const el = h('div', {
    class: ['meter', `meter-${size}`, idle && 'is-idle'],
    role: 'progressbar', 'aria-label': label, 'aria-valuemin': 0, 'aria-valuemax': 100,
  });
  setMeter(el, pct, idle);
  return el;
}

export function setMeter(el, pct, idle = false) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  el.style.setProperty('--p', `${p}%`);
  el.classList.toggle('is-idle', !!idle);
  if (idle) el.removeAttribute('aria-valuenow');
  else el.setAttribute('aria-valuenow', String(Math.round(p)));
}

export function led(state = 'off', { blink = false } = {}) {
  return h('span', { class: ['led', state !== 'off' && state, blink && 'blink'], 'aria-hidden': 'true' });
}

export function statusLed(job) {
  switch (job.status) {
    case 'running': return led('on', { blink: true });
    case 'queued': return led('dim');
    case 'done': return led('ok');
    case 'failed': return led('bad');
    default: return led('off');
  }
}

export function statusWord(job) {
  return { queued: 'Waiting', running: 'Working', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' }[job.status] || job.status;
}

/** A titled card on a page (job results, settings sections). */
export function block(title, ...children) {
  return h('section', { class: 'block' }, title ? h('h2', { class: 'block-title' }, title) : null, ...children);
}

export function callout(level, title, message, extra = null) {
  const ico = level === 'danger' || level === 'warning' ? 'alert' : level === 'ok' ? 'check' : 'info';
  return h('div', { class: ['callout', `callout-${level}`] },
    icon(ico, 18, 'callout-ico'),
    h('div', { class: 'callout-body' },
      title ? h('strong', null, title) : null,
      message ? h('p', null, message) : null,
      extra));
}
