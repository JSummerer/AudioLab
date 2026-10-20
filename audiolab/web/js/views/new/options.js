// New view — what to do: the four tasks (Save/Convert · Enhance · Split ·
// Check) and the options of the chosen task. Rarely used choices and fine-tuning
// sit under each task's "More options"; whatever is set there is sent.
import { h, icon, clear } from '../../dom.js';
import { store, aiAvailable, deviceLabel, isShown, setShown, formatInfo } from '../../store.js';
import { segmented, chips, switchControl, autoRange, toast, callout } from '../../ui.js';
import { go, settingsPath } from '../../router.js';
import { state, refs, redraw, definePart, isFileLike, isLinkSource, appliedAdvanced, TASKS, LIGHT_KEYS, RECIPE_LABELS } from './state.js';
import { tracksBlock } from './tracks.js';
import { hasOwnCover } from './cover.js';
import { TASK_ICON } from '../job/index.js';

// Destination presets: what people own, not codec names (the format itself is the
// chip's second line and its (i) card — all from /api/health).
const PRESETS = [
  ['mp3_320', 'Phone & everyday'],
  ['wav16', 'DJ USB / car'],
  ['alac16', 'Apple Music'],
  ['flac24', 'Archive'],
];

// Rarely used choices: under "More options" (unless already picked).
const RARE_DOWNLOAD = new Set(['opus']);
// Split file types shown up front, with short labels; the others are rare.
const SPLIT_SHORT = { wav16: 'WAV', flac24: 'FLAC', mp3_320: 'MP3' };

definePart('tiles', renderTiles);
definePart('options', renderOptions);

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------
/** Why a task can't be picked right now ({ reason, action? }), or null. */
export function taskBlock(key) {
  const src = state.source;
  if (key === 'separate' && !aiAvailable()) {
    if (store.health?.ai?.install_supported === false) {
      return { reason: store.health.ai.reason || 'AI is unavailable in this build', action: 'ai' };
    }
    return store.health?.ai?.installing
      ? { reason: 'The AI add-on is installing…', action: 'ai' }
      : { reason: 'Needs the free AI add-on — tap to set up', action: 'ai' };
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

// Four entries under a rule: the task's icon and name, one line; the picked one in the accent.
function renderTiles() {
  const el = clear(refs.tiles);
  if (taskBlock(state.task) && !taskBlock(state.task).action) state.task = 'download';
  for (const t of TASKS) {
    const block = taskBlock(t.key);
    const { title, desc } = taskText(t);
    const selected = state.task === t.key;
    const tile = h('button', {
      type: 'button', class: ['tile', block && 'is-blocked'],
      'aria-pressed': String(selected && !block), 'aria-disabled': block ? 'true' : null,
      dataset: { task: t.key },
    },
    h('span', { class: 'tile-title' }, icon(TASK_ICON[t.key], 17, 'tile-ico'), title),
    h('span', { class: ['tile-desc', block && 'is-reason'] }, block ? block.reason : desc));
    tile.addEventListener('click', () => {
      if (block) {
        if (block.action === 'ai') go(settingsPath('ai'), { focus: false });
        else toast(block.reason);
        return;
      }
      if (state.task === t.key) return;
      state.task = t.key;
      redraw('tiles', 'options', 'action');
    });
    el.append(tile);
  }
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------
function optionBlock(label, ...content) {
  return h('div', { class: 'opt' }, h('div', { class: 'opt-label' }, label), ...content);
}

function advRows(rows) {
  return h('div', { class: 'adv-rows' }, rows.map(([label, help, control]) =>
    h('div', { class: 'adv-row' },
      h('div', { class: 'adv-text' }, h('div', { class: 'adv-label' }, label), h('div', { class: 'adv-help' }, help)),
      h('div', { class: 'adv-control' }, control))));
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
 * "Cover art" (More options of Save and Enhance). `chosen()`: the picked formats (Health
 * entries) — the help names those that can't hold a picture. An own image is always used.
 */
let syncCover = null; // refreshes that help after a format change

function coverRow(o, chosen) {
  const own = hasOwnCover();
  const help = h('span', { class: 'switch-row-help', id: 'coverArtHelp' });
  syncCover = () => {
    const none = chosen().filter((f) => f.cover === false).map((f) => f.label);
    help.textContent = [
      own ? 'Your own image is always added.'
        : isFileLike() ? 'Keeps the picture the file already has.' : 'Adds the video thumbnail as album art.',
      none.length ? `No cover in ${none.join(', ')}.` : null,
    ].filter(Boolean).join(' ');
  };
  syncCover();
  const sw = switchControl({
    id: 'coverArt', checked: own || o.embed_cover, disabled: own, label: 'Cover art',
    onChange: (v) => { o.embed_cover = v; syncMore?.(); },
  });
  sw.setAttribute('aria-describedby', help.id);
  return h('label', { class: 'switch-row', for: sw.id }, sw, h('span', { class: 'switch-row-label' }, 'Cover art'), help);
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

// --- More options ----------------------------------------------------------------
// Open or closed is remembered per task (at first: Settings → "Show all options by
// default"). Folded, the head says how many differ from their defaults, with Reset.
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
    redraw('options', 'action');
    refs.options.querySelector('.more .more-toggle')?.focus();
  });
  syncMore = sync;
  sync();
  return h('div', { class: 'more' }, h('div', { class: 'more-head' }, toggle, status), panel);
}

function renderOptions() {
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
  el.append(optionBlock(title, fmt.main, sourceNote()));
  const tb = tracksBlock();
  if (tb) el.append(tb);
  el.append(moreOptions('download', [
    fmt.more ? optionBlock('Other formats', fmt.more) : null,
    coverRow(d, () => choices.filter((f) => f.key === d.format)),
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
  const tb = tracksBlock();
  el.append(
    optionBlock('Type of audio',
      segmented({ label: 'Type of audio', value: e.recipe, options: recipeOptions(), onChange: (v) => { e.recipe = v; redraw('sleeve'); } })),
    optionBlock('Quality',
      segmented({
        label: 'Quality', value: e.quality,
        options: [
          { value: 'light', label: 'Light', desc: 'Volume only · keeps the sound' },
          { value: 'standard', label: 'Standard', desc: 'Fast · works on any computer' },
          {
            value: 'best', label: 'Best', disabled: !ai,
            desc: ai ? `Uses AI · slower${store.health?.ai?.device ? ` · ${deviceLabel(store.health.ai.device)}` : ''}` : null,
            reason: store.health?.ai?.install_supported === false ? 'AI is unavailable in this build' : 'Needs the AI add-on',
          },
        ],
        onChange: (v) => {
          const wasLight = e.quality === 'light';
          e.quality = v;
          // Light takes fewer fine-tuning settings.
          if (wasLight !== (v === 'light')) refs.options.querySelector('.fine-tuning')?.replaceWith(fineTuning());
          redraw('action');
          syncMore?.();
        },
      })),
    optionBlock('Save for', all.length ? fmts.main : loadingNote(), sourceNote()),
  );
  if (tb) el.append(tb);
  el.append(moreOptions('enhance', [
    fmts.more ? optionBlock('More formats', fmts.more) : null,
    coverRow(e, () => all.filter((f) => e.formats.has(f.key))),
    fineTuning(),
  ], {
    changed: () => formatsChanged().length + Number(e.embed_cover !== dflt.embed_cover) + appliedAdvanced().length,
    reset: () => {
      formatsChanged().forEach((k) => state.enhance.formats.delete(k));
      if (!state.enhance.formats.size) state.enhance.formats = new Set(dflt.enhance_formats);
      e.embed_cover = dflt.embed_cover;
      state.enhance.advanced = {};
    },
  }));
}

function fineTuning() {
  const a = state.enhance.advanced;
  const set = (k) => (v) => { if (v == null || v === 'auto') delete a[k]; else a[k] = v; };
  const tri = (k, opts) => segmented({
    label: k, size: 'sm', value: a[k] ?? 'auto',
    options: opts.map(([value, label]) => ({ value, label })), onChange: set(k),
  });
  const light = state.enhance.quality === 'light';
  const rows = [
    ['target_lufs', 'Loudness target', 'How loud the result is. Streaming apps play near −14 LUFS; DJ sets sit around −10 to −12.',
      autoRange({ min: -24, max: -6, step: 0.5, value: a.target_lufs ?? null, unit: 'LUFS', fallback: -14, onChange: set('target_lufs') })],
    ['tp_ceiling_db', 'Peak ceiling', 'Highest allowed peak (true peak). Lower is safer for phones and Bluetooth.',
      autoRange({ min: -3, max: -0.1, step: 0.1, value: a.tp_ceiling_db ?? null, unit: 'dBTP', fallback: -1, onChange: set('tp_ceiling_db') })],
    ['bass_db', 'Bass boost', 'Extra low end. 0 keeps the bass as it is.',
      autoRange({ min: 0, max: 6, step: 0.5, value: a.bass_db ?? null, unit: 'dB', fallback: 1.5, onChange: set('bass_db') })],
    ['glue', 'Glue compression', 'Gently evens out loud and quiet moments in a mix.', tri('glue', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['hf_restore', 'Restore highs', 'Rebuilds missing top end on compressed uploads. Can sound brighter than the source.', tri('hf_restore', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['denoise', 'Noise reduction', 'Off by default. Choose Light or Strong only if you hear steady hiss or hum.',
      segmented({
        label: 'Noise reduction', size: 'sm', value: ['light', 'strong'].includes(a.denoise) ? a.denoise : 'off',
        options: [['off', 'Off'], ['light', 'Light'], ['strong', 'Strong']].map(([value, label]) => ({ value, label })),
        onChange: (v) => set('denoise')(v === 'off' ? null : v),
      })],
    ['mono_bass', 'Mono bass', 'Makes deep bass mono, as club systems expect.', tri('mono_bass', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']])],
    ['sample_rate', 'Sample rate', 'Higher rates don’t add detail; 48 kHz is the safe choice.',
      segmented({
        label: 'Sample rate', size: 'sm', value: a.sample_rate ?? 'auto',
        options: [['auto', 'Auto'], [44100, '44.1 kHz'], [48000, '48 kHz'], [96000, '96 kHz']].map(([value, label]) => ({ value, label })),
        onChange: set('sample_rate'),
      })],
  ];
  const el = optionBlock('Fine-tuning',
    h('p', { class: 'muted small adv-note' }, light
      ? 'Light only changes the volume, so only these settings apply.'
      : 'Keep the defaults unless you know you want something else. Noise reduction is off until you choose it.'),
    advRows((light ? rows.filter(([key]) => LIGHT_KEYS.includes(key)) : rows).map(([, ...rest]) => rest)));
  el.classList.add('fine-tuning');
  return el;
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
    models.length ? advRows([
      ['AI model', 'The fine-tuned model is cleaner but about four times slower.',
        segmented({
          label: 'AI model', size: 'sm', value: s.model,
          options: [{ value: 'auto', label: 'Automatic' }, ...models.map((m) => ({ value: m.key, label: m.label }))],
          onChange: (v) => { s.model = v; },
        })],
    ]) : null,
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
