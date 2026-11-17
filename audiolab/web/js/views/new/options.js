// New view — what to do: the four tasks (Save/Convert · Enhance · Split ·
// Check) and the options of the chosen task. Rarely used choices, the name and cover
// tools and fine-tuning sit under each task's "More options"; whatever is set there is sent.
// Every option reads like a sleeve's credits: its label left, its control right.
import { h, icon, clear } from '../../dom.js';
import { RECIPE_LABELS, store, aiAvailable, isShown, setShown, formatInfo, ENHANCE_PRESETS } from '../../store.js';
import { segmented, chips, switchControl, autoRange, toast, callout } from '../../ui.js';
import { settingsPath } from '../../router.js';
import { state, refs, redraw, definePart, isFileLike, isLinkSource, appliedAdvanced, TASKS, LIGHT_KEYS, PROCESSING_LABELS } from './state.js';
import { tracksBlock, tracksUpFront } from './tracks.js';
import { hasOwnCover, takesCover, coverTools } from './cover.js';
import { nameTools } from './names.js';
import { TASK_ICON } from '../job/index.js';
import { eqField, formatEq, parseEq } from '../../eq.js';
import { sectionEqField, sectionRows, checkSections } from '../../section-eq.js';

// Destination presets: what people own, not codec names (the format itself is the
// chip's second line and its (i) card — all from /api/health). Shared with Settings.
const PRESETS = ENHANCE_PRESETS;

// Rarely used choices: under "More options" (unless already picked).
const RARE_DOWNLOAD = new Set(['opus']);
// Split file types shown up front, with short labels; the others are rare.
const SPLIT_SHORT = { wav16: 'WAV', flac24: 'FLAC', mp3_320: 'MP3' };

definePart('tiles', renderTiles);
definePart('options', renderOptions);

// ---------------------------------------------------------------------------
// Tasks: a radio group (one Tab stop, arrow keys move and pick). A task that needs the
// AI add-on is a plain link to Settings → AI until it is installed.
// ---------------------------------------------------------------------------
/** Why a task can't be picked right now ({ reason, action? }), or null. */
export function taskBlock(key) {
  const src = state.source;
  if (key === 'separate' && !aiAvailable()) {
    if (store.health?.ai?.install_supported === false) {
      return { reason: store.health.ai.reason || 'AI is unavailable in this build' };
    }
    return store.health?.ai?.installing
      ? { reason: 'The AI add-on is installing…', action: 'ai' }
      : { reason: 'Needs the free AI add-on', action: 'ai' };
  }
  if (key === 'analyze' && src?.kind === 'playlist') return { reason: 'Pick a single video to check it' };
  return null;
}

function taskText(t) {
  if (t.key === 'download') {
    return isFileLike()
      ? { title: 'Convert', desc: 'Turn it into another format' }
      : { title: 'Save', desc: 'Download the audio or video' };
  }
  return { title: t.title, desc: t.desc };
}

let keyedTiles = null; // the tile group that has its arrow-key handler

// Four entries under a rule: the task's icon and name, one line; the picked one in the accent.
function renderTiles() {
  const el = refs.tiles;
  if (keyedTiles !== el) { el.addEventListener('keydown', onTileKey); keyedTiles = el; }
  const focused = el.contains(document.activeElement) ? document.activeElement.dataset?.task : null;
  clear(el);
  // A task that can't be picked now doesn't stay picked: files start on Enhance, links on Save.
  if (taskBlock(state.task)) state.task = isFileLike() && !taskBlock('enhance') ? 'enhance' : 'download';
  for (const t of TASKS) {
    const block = taskBlock(t.key);
    const { title, desc } = taskText(t);
    const head = h('span', { class: 'tile-title' }, icon(TASK_ICON[t.key], 17, 'tile-ico'), title);
    if (block?.action === 'ai') {
      el.append(h('a', { class: 'tile is-blocked', href: settingsPath('ai'), dataset: { task: t.key } },
        head, h('span', { class: 'tile-desc is-reason' }, block.reason),
        h('span', { class: 'tile-link' }, store.health?.ai?.installing ? 'See the progress…' : 'Set up AI…')));
      continue;
    }
    const selected = state.task === t.key && !block;
    const tile = h('button', {
      type: 'button', role: 'radio', class: ['tile', block && 'is-blocked'],
      'aria-checked': String(selected), 'aria-disabled': block ? 'true' : null, tabindex: selected ? '0' : '-1',
      dataset: { task: t.key },
    },
    head,
    h('span', { class: ['tile-desc', block && 'is-reason'] }, block ? block.reason : desc));
    tile.addEventListener('click', () => pickTask(t.key));
    el.append(tile);
  }
  if (focused) el.querySelector(`[data-task="${focused}"]`)?.focus({ preventScroll: true });
}

function pickTask(key) {
  const block = taskBlock(key);
  if (block) { toast(block.reason); return; }
  state.taskPicked = true;
  if (state.task === key) return;
  state.task = key;
  redraw('tiles', 'options', 'action');
}

/** Arrow keys, Home and End move between the tasks that can be picked, and pick them (as radio buttons do). */
function onTileKey(e) {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  const radios = [...refs.tiles.querySelectorAll('[role="radio"]:not([aria-disabled="true"])')];
  const i = radios.indexOf(document.activeElement);
  if (i < 0 || !(step || e.key === 'Home' || e.key === 'End')) return;
  e.preventDefault();
  const next = step ? radios[(i + step + radios.length) % radios.length] : e.key === 'Home' ? radios[0] : radios.at(-1);
  next.focus();
  pickTask(next.dataset.task);
}

// ---------------------------------------------------------------------------
// Options: one label column and one control column
// ---------------------------------------------------------------------------
/** One option: its label, then its control(s). `text`: the first control is a line of text, not a pill row. */
function optionBlock(label, ...content) {
  return h('div', { class: 'opt' }, h('div', { class: 'opt-label' }, label), ...content);
}

function textOption(label, ...content) {
  const el = optionBlock(label, ...content);
  el.classList.add('opt-text');
  return el;
}

export function downloadChoices() {
  const list = store.health?.formats?.download || [];
  return isFileLike() ? list.filter((f) => !['original', 'video'].includes(f.key)) : list;
}

/** The format pickers come from /api/health, which is normally there before New shows. */
const loadingNote = () => h('p', { class: 'muted small' }, 'Loading formats…');

/** Under the Save / Enhance picker for links: what the link's audio really is. */
function sourceNote() {
  const note = isLinkSource() && store.health?.format_notes?.source;
  return note ? h('p', { class: 'muted small format-note' }, note) : null;
}

/**
 * "Embed cover art" (More options of Save and Enhance). `chosen()`: the picked formats
 * (Health entries) — the help names AudioLab's unsupported targets. An own image overrides the switch.
 */
let syncCover = null; // refreshes that help after a format change

function coverRow(o, chosen) {
  const own = hasOwnCover();
  const help = h('span', { class: 'switch-row-help', id: 'coverArtHelp' });
  syncCover = () => {
    const selected = chosen();
    const supported = selected.filter((f) => f.cover === true).map((f) => f.label);
    const none = selected.filter((f) => f.cover === false).map((f) => f.label);
    let sourceHelp = isFileLike() ? 'Keeps existing album art inside the audio when available.'
      : 'Embeds the video thumbnail inside the audio when available.';
    if (own) {
      sourceHelp = supported.length ? `Your image is embedded in ${supported.join(', ')}.`
        : 'Choose a supported audio format to embed your image.';
    }
    help.textContent = [
      sourceHelp,
      none.length ? `AudioLab adds no cover art to ${none.join(', ')}.` : null,
    ].filter(Boolean).join(' ');
  };
  syncCover();
  const sw = switchControl({
    id: 'coverArt', checked: own || o.embed_cover, disabled: own, label: 'Embed cover art',
    onChange: (v) => { o.embed_cover = v; syncMore?.(); redraw('sleeve'); },
  });
  sw.setAttribute('aria-describedby', help.id);
  return h('label', { class: 'switch-row', for: sw.id }, sw, h('span', { class: 'switch-row-label' }, 'Embed cover art'), help);
}

/**
 * Under More options, for one link or file: its name (file names and tags) and its cover art —
 * `embed` (the Embed cover art switch, Save and Enhance), an own picture, and the separate image.
 */
function sourceOptions(task, embed = null) {
  const src = state.source;
  if (!takesCover(src)) return embed ? [textOption('Cover art', embed)] : [];
  const refresh = () => { syncMore?.(); redraw('sleeve'); };
  const cover = [embed, ...coverTools(src, task, { onChange: refresh })].filter(Boolean);
  return [
    textOption('Name', nameTools(src)),
    cover.length ? textOption('Cover art', ...cover) : null,
  ];
}

/** Settings → Defaults: what the options start from, and what "Reset" returns to. */
function defaults() {
  const s = store.settings || {};
  return {
    download_format: s.download_format || 'mp3',
    embed_cover: s.embed_cover !== false,
    enhance_formats: s.enhance_formats?.length ? s.enhance_formats : ['mp3_320', 'wav16'],
    separate_format: s.separate_format || 'wav16',
  };
}

/**
 * One choice as two rows of pills: the usual options, and the rare ones for
 * "More options" (`more` is null when there are none). A rare option that is
 * already picked stays with the usual ones, so the choice is never folded away.
 */
function splitChoice({ label, moreLabel, value, options, rare, onChange }) {
  const usual = options.filter((o) => !rare.has(o.value) || o.value === value);
  const extra = options.filter((o) => !usual.includes(o));
  // Two radio groups, one value: picking in one clears the other (and its note line).
  const pick = (other) => (v) => {
    const group = other();
    group?.querySelectorAll('input').forEach((i) => { i.checked = false; });
    group?.sync?.();
    onChange(v);
    syncMore?.();
  };
  let more = null;
  const main = segmented({ label, value, options: usual, onChange: pick(() => more) });
  if (extra.length) more = segmented({ label: moreLabel, value, options: extra, onChange: pick(() => main) });
  return { main, more, moreKeys: new Set(extra.map((o) => o.value)) };
}

/** Tracks beside the other choices when they matter (tracks.js tracksUpFront), else under More options. */
function placeTracks() {
  const tb = tracksBlock();
  if (tb) tb.classList.add('opt-text');
  return tb && tracksUpFront() ? { front: tb, folded: null } : { front: null, folded: tb };
}

// --- More options ----------------------------------------------------------------
// Open or closed is remembered per task (at first: Settings → "Show all options by
// default"). Folded, the head says how many differ from their defaults, with Reset. Reset is for the
// processing options only: the name and the cover picture stay as the page shows them (and as they are sent).
let syncMore = null; // refreshes the folded head after a change outside the fold

function moreOptions(task, body, { changed, reset }) {
  const key = `more.${task}`;
  const panel = h('div', { class: 'more-body', id: `more-${task}` }, body);
  const toggle = h('button', { type: 'button', class: 'more-toggle', 'aria-controls': panel.id },
    h('span', { class: 'more-mark', 'aria-hidden': 'true' }), 'More options');
  const count = h('span');
  const resetBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, 'Reset');
  const status = h('span', { class: 'more-status' }, count, ' · ', resetBtn);
  const sync = () => {
    const open = isShown(key);
    toggle.setAttribute('aria-expanded', String(open));
    panel.hidden = !open;
    const n = open ? 0 : changed();
    status.hidden = !n;
    count.textContent = `${n} changed`;
    resetBtn.setAttribute('aria-label', `Reset ${n} changed option${n === 1 ? '' : 's'}`);
  };
  toggle.addEventListener('click', () => { setShown(key, panel.hidden); sync(); });
  resetBtn.addEventListener('click', () => {
    reset();
    redraw('source', 'options', 'action');
    refs.options.querySelector('.more .more-toggle')?.focus();
  });
  syncMore = sync;
  sync();
  return h('div', { class: 'more' }, h('div', { class: 'more-head' }, toggle, status), panel);
}

function renderOptions() {
  // Don't rebuild under someone typing a name (health and settings events redraw).
  if (state.source?.editing && document.activeElement?.closest?.('.name-edit') && refs.options.contains(document.activeElement)) return;
  // Rebuilt under someone typing (the tracklist, on a health or settings event)? Keep their place.
  const ta = refs.options.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA' ? document.activeElement : null;
  const place = ta && [ta.id, ta.selectionStart, ta.selectionEnd, ta.scrollTop];
  const el = clear(refs.options);
  syncMore = null;
  syncCover = null;
  el.className = `options options-${state.task}`;
  if (state.task === 'download') renderDownloadOptions(el);
  else if (state.task === 'enhance') renderEnhanceOptions(el);
  else if (state.task === 'separate') renderSeparateOptions(el);
  else renderAnalyzeOptions(el);
  const again = place && document.getElementById(place[0]);
  if (again) {
    again.focus({ preventScroll: true });
    again.setSelectionRange(place[1], place[2]);
    again.scrollTop = place[3];
  }
}

function renderDownloadOptions(el) {
  const d = state.download;
  const dflt = defaults();
  const choices = downloadChoices();
  const title = isFileLike() ? 'Convert to' : 'Save as';
  if (!choices.length) { el.append(optionBlock(title, loadingNote())); return; }
  if (!choices.some((c) => c.key === d.format)) d.format = choices[0].key;
  const coverOn = () => d.embed_cover || hasOwnCover();
  const fmt = splitChoice({
    label: 'Format', moreLabel: 'Other formats', value: d.format, rare: RARE_DOWNLOAD,
    options: choices.map((f) => ({ value: f.key, label: f.label, desc: f.description, info: () => formatInfo(f, coverOn()) })),
    onChange: (v) => { d.format = v; syncCover?.(); redraw('action'); },
  });
  const formatChanged = () => fmt.moreKeys.has(d.format) && d.format !== dflt.download_format;
  const tracks = placeTracks();
  el.append(optionBlock(title, fmt.main, sourceNote()));
  if (tracks.front) el.append(tracks.front);
  el.append(moreOptions('download', [
    tracks.folded,
    fmt.more ? optionBlock('Other formats', fmt.more) : null,
    ...sourceOptions('download', coverRow(d, () => choices.filter((f) => f.key === d.format))),
  ], {
    changed: () => Number(formatChanged()) + Number(d.embed_cover !== dflt.embed_cover),
    reset: () => {
      if (formatChanged()) d.format = dflt.download_format;
      d.embed_cover = dflt.embed_cover;
    },
  }));
}

// --- Enhance -------------------------------------------------------------------
function recipeOptions() {
  const recipes = store.health?.recipes?.length
    ? store.health.recipes
    : Object.entries(RECIPE_LABELS).map(([key, name]) => ({ key, name }));
  const guess = state.source?.kind === 'url' ? state.source.probe?.guess
    : state.source?.kind === 'job' ? state.source.job?.result?.classification : null;
  const autoDesc = guess && guess.type && guess.type !== 'unknown'
    ? `Looks like: ${guess.label}` : 'Detects what it is';
  return [
    { value: 'auto', label: 'Auto', desc: autoDesc },
    ...recipes.map((r) => ({ value: r.key, label: RECIPE_LABELS[r.key] || r.name, desc: r.short })),
  ];
}

/** Enhance format keys in the order the chips show them: the presets, then Health's order. */
export function enhanceFormatsInOrder(keys) {
  const order = [...PRESETS.map(([key]) => key), ...(store.health?.formats?.enhance || []).map((f) => f.key)];
  const rank = (k) => (order.includes(k) ? order.indexOf(k) : order.length);
  return [...keys].sort((a, b) => rank(a) - rank(b));
}

/** The presets as chips, plus the other formats for "More options" (ticked ones stay up front). */
function enhanceFormatGroups() {
  const e = state.enhance;
  const all = store.health?.formats?.enhance || [];
  const coverOn = () => e.embed_cover || hasOwnCover();
  const option = (f, label, desc) => ({ value: f.key, label, desc, info: () => formatInfo(f, coverOn()) });
  const usual = PRESETS.flatMap(([key, label]) => all.filter((f) => f.key === key).map((f) => option(f, label, f.label)));
  const rest = all.filter((f) => !PRESETS.some(([key]) => key === f.key)).map((f) => option(f, f.label, f.description));
  usual.push(...rest.filter((o) => e.formats.has(o.value)));
  const extra = rest.filter((o) => !e.formats.has(o.value));
  // Two chip groups, one Set: each group only adds or removes its own formats.
  const group = (label, options) => {
    const own = new Set(options.map((o) => o.value));
    return chips({
      label, values: e.formats, options,
      onChange: (next) => {
        e.formats = new Set([...[...e.formats].filter((k) => !own.has(k)), ...[...next].filter((k) => own.has(k))]);
        redraw('action');
        syncMore?.();
        syncCover?.();
      },
    });
  };
  return { main: group('Save for', usual), more: extra.length ? group('More formats', extra) : null, moreKeys: new Set(extra.map((o) => o.value)) };
}

function renderEnhanceOptions(el) {
  const e = state.enhance;
  const dflt = defaults();
  const ai = aiAvailable();
  if (!ai && e.quality === 'best') e.quality = 'standard';
  const all = store.health?.formats?.enhance || [];
  const fmts = enhanceFormatGroups();
  const formatsChanged = () => [...fmts.moreKeys].filter((k) => state.enhance.formats.has(k) && !dflt.enhance_formats.includes(k));
  const tracks = placeTracks();
  el.append(
    optionBlock('Type of audio',
      segmented({ label: 'Type of audio', value: e.recipe, options: recipeOptions(), onChange: (v) => { e.recipe = v; redraw('sleeve'); } })),
    optionBlock('Processing',
      segmented({
        label: 'Processing', value: e.quality,
        options: [
          { value: 'light', label: PROCESSING_LABELS.light, desc: 'Sets the loudness and keeps peaks in check' },
          { value: 'standard', label: PROCESSING_LABELS.standard, desc: 'Adjusts tone and level to suit the recording' },
          {
            value: 'best', label: PROCESSING_LABELS.best, disabled: !ai,
            desc: ai ? 'Treats parts separately where the recipe supports it. Slower and can change the tone.' : null,
            reason: store.health?.ai?.install_supported === false ? 'AI is unavailable in this build' : 'Needs the AI add-on',
          },
        ],
        onChange: (v) => {
          const wasLight = e.quality === 'light';
          e.quality = v;
          // Volume only takes fewer fine-tuning settings.
          if (wasLight !== (v === 'light')) refs.options.querySelector('.fine-tuning')?.replaceWith(fineTuning());
          redraw('action');
          syncMore?.();
        },
      })),
    optionBlock('Save for', all.length ? fmts.main : loadingNote(), sourceNote()),
  );
  if (tracks.front) el.append(tracks.front);
  el.append(moreOptions('enhance', [
    tracks.folded,
    fmts.more ? optionBlock('More formats', fmts.more) : null,
    ...sourceOptions('enhance', coverRow(e, () => all.filter((f) => e.formats.has(f.key)))),
    fineTuning(),
  ], {
    changed: () => formatsChanged().length + Number(e.embed_cover !== dflt.embed_cover) + fineTuningChanges(),
    reset: () => {
      formatsChanged().forEach((k) => state.enhance.formats.delete(k));
      if (!state.enhance.formats.size) state.enhance.formats = new Set(dflt.enhance_formats);
      e.embed_cover = dflt.embed_cover;
      state.enhance.advanced = {};
      delete state.enhance.eqText;
      delete state.enhance.sectionRows;
    },
  }));
}

/**
 * Fine-tuning: each setting is an option like the others (label left, control and its help right).
 * Choices are pills with the default first; on/off is a switch; a number is Auto or Custom with a slider;
 * the EQs are editors.
 */
function fineTuning() {
  const a = state.enhance.advanced;
  const set = (k) => (v) => {
    const before = fineTuningChanges();
    if (v == null || v === 'auto') delete a[k]; else a[k] = v;
    if (fineTuningChanges() !== before) redraw('sleeve'); // it counts them
  };
  const pills = (k, label, opts, dflt = 'auto') => segmented({
    label, size: 'sm', value: opts.some(([v]) => String(v) === String(a[k])) ? a[k] : dflt,
    options: opts.map(([value, text]) => ({ value, label: text })),
    onChange: (v) => set(k)(String(v) === String(dflt) ? null : v),
  });
  const number = (k, label, range) => autoRange({ ...range, label, value: a[k] ?? null, onChange: set(k) });
  const light = state.enhance.quality === 'light';
  const rows = [
    ['target_lufs', 'Loudness target', 'How loud the result is. Streaming apps play near −14 LUFS; DJ sets sit around −10 to −12.',
      number('target_lufs', 'Loudness target', { min: -24, max: -6, step: 0.5, unit: 'LUFS', fallback: -14 })],
    ['tp_ceiling_db', 'Peak ceiling', 'Highest allowed peak (true peak) of the master; lossy files may use the room up to just under full scale. Lower is safer for phones and Bluetooth.',
      number('tp_ceiling_db', 'Peak ceiling', { min: -3, max: -0.1, step: 0.1, unit: 'dBTP', fallback: -1 })],
    ['bass_db', 'Bass boost', 'Extra low end. 0 keeps the bass as it is.',
      number('bass_db', 'Bass boost', { min: 0, max: 6, step: 0.5, unit: 'dB', fallback: 1.5 })],
    ['harmonics', 'Bass harmonics', 'Adds harmonics so deep bass is easier to hear on small speakers. Off by default.',
      switchControl({ label: 'Bass harmonics', checked: a.harmonics === 'on', onChange: (on) => set('harmonics')(on ? 'on' : null) })],
    ['adaptive_lowmid', 'Adaptive low-mids', 'Tames brief buildup in the low-mids while keeping steady warmth. Strong allows a deeper cut. Skipped when AI shapes the stems.',
      pills('adaptive_lowmid', 'Adaptive low-mids', [['auto', 'Auto'], ['strong', 'Strong'], ['off', 'Off']])],
    ['glue', 'Glue compression', 'Gently evens out loud and quiet moments in a mix.',
      pills('glue', 'Glue compression', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['hf_restore', 'Brighten muffled highs', 'Only for files whose highs are cut off, like old MP3s. Adds new high harmonics; it can’t bring back lost detail.',
      pills('hf_restore', 'Brighten muffled highs', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['denoise', 'Noise reduction', 'Choose Light or Strong only if you hear steady hiss or hum.',
      pills('denoise', 'Noise reduction', [['off', 'Off'], ['light', 'Light'], ['strong', 'Strong']], 'off')],
    ['mono_bass', 'Mono bass', 'Makes deep bass mono, as club systems expect.',
      pills('mono_bass', 'Mono bass', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['deess', 'Sharp S sounds', 'Softens S sounds and cymbal hits only where they jump out. Strong suits harsh recordings.',
      pills('deess', 'Sharp S sounds', [['auto', 'Auto'], ['strong', 'Strong'], ['off', 'Off']])],
    ['sample_rate', 'Sample rate', '48 kHz suits TVs, phones and most players; 44.1 kHz is for CDs. Higher rates don’t add detail.',
      pills('sample_rate', 'Sample rate', [['auto', 'Auto'], [44100, '44.1 kHz'], [48000, '48 kHz'], [96000, '96 kHz']])],
    ['eq', 'Custom EQ', 'For experts. Up to 8 bands, one per line, like 160 Hz −3.3 dB Q 2.9. Applied before compression, loudness and peak control.',
      eqControl()],
    ['section_eq', 'Section EQ', 'For experts. EQ for parts of the recording, like a harsh stretch of a long set. Each part fades in and out (5 seconds unless you set a fade). Applied after Custom EQ.',
      sectionControl()],
  ];
  const shown = light ? rows.filter(([key]) => LIGHT_KEYS.includes(key)) : rows;
  return h('div', { class: 'fine-tuning' },
    textOption('Fine-tuning', h('p', { class: 'muted small' }, light
      ? 'Only these settings apply when you choose Volume only.'
      : 'Keep the defaults unless you know you want something else.')),
    shown.map(([key, label, help, control]) => {
      const row = optionBlock(label, control, h('p', { class: 'muted small tune-help' }, help));
      row.classList.add('tune');
      row.dataset.key = key;
      return row;
    }));
}

/**
 * Custom EQ: the text is kept as typed (`eqText`, it survives redraws); only text that reads
 * cleanly becomes `advanced.eq`. Until then Start waits (eqProblem), except in Volume only.
 */
function eqControl() {
  const e = state.enhance;
  return eqField({
    id: 'eqText', text: e.eqText ?? formatEq(e.advanced.eq), label: 'Custom EQ',
    onInput: ({ text, bands, errors }) => {
      const blocked = !!eqProblem();
      const before = fineTuningChanges();
      e.eqText = text;
      if (bands.length && !errors.length) e.advanced.eq = bands;
      else delete e.advanced.eq;
      if (blocked !== !!eqProblem()) redraw('action'); // the sleeve follows
      else if (before !== fineTuningChanges()) redraw('sleeve');
      syncMore?.();
    },
  }).el;
}

/**
 * Section EQ: the rows are kept as typed (`sectionRows`); only sections that all check out become
 * `advanced.section_eq`. Until then Start waits (sectionProblem), except in Volume only.
 */
function sectionControl() {
  const e = state.enhance;
  e.sectionRows ||= sectionRows(e.advanced.section_eq);
  // The editor changes the rows before it calls back, so the state before an edit is kept here.
  let blocked = !!sectionProblem();
  let count = fineTuningChanges();
  return sectionEqField({
    rows: e.sectionRows, duration: sourceDuration(), timeline: sourceTimeline,
    onInput: (rows, { sections, errors }) => {
      if (sections.length && !errors.length) e.advanced.section_eq = sections;
      else delete e.advanced.section_eq;
      const [wasBlocked, was] = [blocked, count];
      [blocked, count] = [!!sectionProblem(), fineTuningChanges()];
      if (blocked !== wasBlocked) redraw('action'); // the sleeve follows
      else if (count !== was) redraw('sleeve');
      syncMore?.();
    },
  }).el;
}

/** The recording's length when the source says it (a looked-up link, an earlier job), else null. */
function sourceDuration() {
  const src = state.source;
  if (src?.kind === 'url') return src.probe?.duration_s || null;
  return src?.kind === 'job' ? src.job?.source?.duration_s || null : null;
}

/**
 * An earlier Check or Enhance job's tone timeline ("Suggest from the chart"), else null. Not for one of its
 * result files: the timeline is of the job's input, so it would suggest what that Enhance already did.
 */
const sourceTimeline = () => (state.source?.kind === 'job' && !state.source.sourceFile
  ? state.source.job?.result?.analysis?.tone_timeline || null : null);

/** Why Start waits for the section EQ, or null. Volume only doesn't use it. */
export function sectionProblem() {
  const e = state.enhance;
  if (state.task !== 'enhance' || e.quality === 'light' || !e.sectionRows?.length) return null;
  const n = checkSections(e.sectionRows, sourceDuration()).errors.length;
  return n ? `Fix the section EQ first: ${n} problem${n === 1 ? '' : 's'}, listed under it.` : null;
}

/** Fine-tuning settings that differ from Auto for the chosen processing; unfinished EQ or sections count too. */
export function fineTuningChanges() {
  return appliedAdvanced().length + Number(!!eqProblem()) + Number(!!sectionProblem());
}

/** Why Start waits for the custom EQ text, or null. Volume only doesn't use it. */
export function eqProblem() {
  const e = state.enhance;
  if (state.task !== 'enhance' || e.quality === 'light' || !e.eqText?.trim()) return null;
  const n = parseEq(e.eqText).errors.length;
  return n ? `Fix the custom EQ first: ${n} problem${n === 1 ? '' : 's'}, listed under it.` : null;
}

// --- Split ---------------------------------------------------------------------
function separateFormatOptions() {
  return (store.health?.formats?.separate || []).map((f) => ({
    value: f.key, label: SPLIT_SHORT[f.key] || f.label, desc: f.description || f.label, info: () => formatInfo(f),
  }));
}

function renderSeparateOptions(el) {
  const s = state.separate;
  const dflt = defaults();
  const options = separateFormatOptions();
  if (options.length && !options.some((f) => f.value === s.format)) s.format = options[0].value;
  const fmt = splitChoice({
    label: 'File type', moreLabel: 'Other file types', value: s.format, options,
    rare: new Set(options.filter((o) => !SPLIT_SHORT[o.value]).map((o) => o.value)),
    onChange: (v) => { s.format = v; redraw('sleeve'); },
  });
  const formatChanged = () => fmt.moreKeys.has(s.format) && s.format !== dflt.separate_format;
  const models = store.health?.ai_models || [];
  el.append(
    optionBlock('Split into',
      segmented({
        label: 'Split into', value: s.mode,
        options: [
          { value: 'karaoke', label: 'Vocals + instrumental', desc: 'A karaoke track and a clean vocal' },
          { value: 'stems4', label: '4 stems', desc: 'Vocals, drums, bass and everything else' },
        ],
        onChange: (v) => { s.mode = v; redraw('action'); },
      })),
    optionBlock('File type', options.length ? fmt.main : loadingNote()),
  );
  if (store.health?.ai?.device === 'cpu') {
    el.append(callout('info', null, 'Splitting runs on your processor here. Expect several minutes per song and much longer for long mixes.'));
  }
  el.append(moreOptions('separate', [
    fmt.more ? optionBlock('Other file types', fmt.more) : null,
    models.length ? optionBlock('AI model',
      segmented({
        label: 'AI model', size: 'sm', value: s.model,
        options: [{ value: 'auto', label: 'Automatic' }, ...models.map((m) => ({ value: m.key, label: m.label }))],
        onChange: (v) => { s.model = v; syncMore?.(); },
      }),
      h('p', { class: 'muted small tune-help' }, 'The fine-tuned model is cleaner but about four times slower.')) : null,
    ...sourceOptions('separate'),
  ], {
    changed: () => Number(formatChanged()) + Number(s.model !== 'auto'),
    reset: () => {
      if (formatChanged()) s.format = dflt.separate_format;
      s.model = 'auto';
    },
  }));
}

// --- Check -----------------------------------------------------------------------
function renderAnalyzeOptions(el) {
  el.append(h('p', { class: 'opt-note' },
    'Measures how loud it is, whether it clips, how dynamic it is and how much of the frequency range is really there — and guesses what kind of audio it is. Nothing is saved.'));
}
