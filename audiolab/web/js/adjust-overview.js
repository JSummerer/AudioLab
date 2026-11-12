import { h, clear, clockText, fmtNum } from './dom.js';
import { sectionFullStrength } from './section-eq.js';

const SVG = 'http://www.w3.org/2000/svg';
const button = (label, onclick) => h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick }, label);
const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

/** An original peak envelope and two separate time lanes. Numeric controls mirror every edit. */
export function adjustmentOverview(duration, { onPassage, onSection, onPick, sectionControls }) {
  let range = null;
  let sections = [];
  let active = null;
  let peaks = null;
  let zoom = null;
  let drag = null;
  let rangeStart = 0;
  let rangeEnd = duration;
  const waveform = h('div', { class: 'adjust-waveform' });
  const audition = h('div', { class: 'adjust-lane adjust-lane-audition' });
  const sectionLane = h('div', { class: 'adjust-lane adjust-lane-sections' });
  const from = h('span');
  const to = h('span');
  const basis = h('p', { class: 'small muted' }, 'Recording timeline. Original peak overview is unavailable for this version.');
  const area = h('div', { class: ['adjust-timeline-area', sectionControls && 'has-sections'], 'aria-hidden': 'true' },
    waveform, audition, sectionControls ? sectionLane : null);
  const zoomBtn = button('Zoom to passage', () => {
    zoom = zoom ? null : passageWindow(range);
    draw();
  });
  const el = h('div', { class: 'adjust-overview' },
    area,
    h('div', { class: 'adjust-timeline-times small muted', 'aria-hidden': 'true' }, from, to),
    h('div', { class: 'adjust-row small' },
      h('span', { class: 'adjust-legend-passage' }, 'Audition'),
      sectionControls ? h('span', { class: 'adjust-legend-section' }, 'EQ sections and fades') : null,
      zoomBtn), basis);

  function xAt(seconds) { return 100 * (seconds - rangeStart) / (rangeEnd - rangeStart); }

  function passageWindow(passage) {
    if (!passage) return null;
    const width = Math.max(45, (passage.end - passage.start) * 3);
    const start = clamp((passage.start + passage.end - width) / 2, 0, Math.max(0, duration - width));
    return [start, Math.min(duration, start + width)];
  }

  function drawWave() {
    clear(waveform);
    if (!peaks?.peaks?.length) return;
    const graph = document.createElementNS(SVG, 'svg');
    graph.setAttribute('viewBox', '0 0 1000 48');
    graph.setAttribute('preserveAspectRatio', 'none');
    const commands = [];
    peaks.peaks.forEach((peak, i) => {
      const begin = i * peaks.bin_duration_s;
      const end = Math.min((i + 1) * peaks.bin_duration_s, peaks.duration_s);
      if (!(end > rangeStart && begin < rangeEnd && Number.isFinite(peak))) return;
      const center = xAt((Math.max(rangeStart, begin) + Math.min(rangeEnd, end)) / 2) * 10;
      const size = 23 * clamp(peak, 0, 1);
      commands.push(`M${center.toFixed(2)} ${(24 - size).toFixed(2)}v${(size * 2).toFixed(2)}`);
    });
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', commands.join(' '));
    graph.append(path);
    waveform.append(graph);
  }

  function strip(lane, bounds, kind, id, selected = false, full = null) {
    if (!(bounds.end > rangeStart && bounds.start < rangeEnd)) return;
    const left = Math.max(0, xAt(bounds.start));
    const right = Math.min(100, xAt(bounds.end));
    if (right <= left) return;
    const bar = h('span', {
      class: ['adjust-strip', `adjust-strip-${kind}`, selected && 'is-active'],
      style: { left: `${left}%`, width: `${right - left}%` },
      dataset: { kind, edge: 'move', id },
    });
    if (full) {
      const l = Math.max(left, xAt(full.start));
      const r = Math.min(right, xAt(full.end));
      if (r > l) bar.append(h('span', { class: 'adjust-full',
        style: { left: `${100 * (l - left) / (right - left)}%`, width: `${100 * (r - l) / (right - left)}%` } }));
    }
    lane.append(bar);
    if (!selected && kind === 'section') return;
    for (const edge of ['start', 'end']) {
      const x = xAt(bounds[edge]);
      if (x < 0 || x > 100) continue;
      lane.append(h('span', {
        class: ['adjust-handle', `adjust-handle-${kind}`, `adjust-handle-${edge}`],
        style: { left: `${x}%` }, dataset: { kind, edge, id },
      }));
    }
  }

  function draw() {
    if (!(duration > 0)) return;
    const [start, end] = zoom || [0, duration];
    const changedScale = start !== rangeStart || end !== rangeEnd;
    rangeStart = start;
    rangeEnd = end;
    if (changedScale) drawWave();
    clear(audition);
    clear(sectionLane);
    if (range) strip(audition, range, 'audition', '', true);
    for (const { id, bounds } of sections) {
      if (!(bounds.end_s > bounds.start_s)) continue;
      const full = bounds.end_s - bounds.start_s > 2 * bounds.fade_s ? sectionFullStrength(bounds) : null;
      strip(sectionLane, { start: bounds.start_s, end: bounds.end_s }, 'section', id, id === active, full);
    }
    from.textContent = clockText(rangeStart);
    to.textContent = clockText(rangeEnd);
    zoomBtn.hidden = duration <= 45;
    zoomBtn.disabled = !range && !zoom;
    zoomBtn.textContent = zoom ? 'Show full recording' : 'Zoom to passage';
  }

  function time(event) {
    const { left, width } = area.getBoundingClientRect();
    return rangeStart + clamp((event.clientX - left) / width, 0, 1) * (rangeEnd - rangeStart);
  }

  area.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || !range || drag) return;
    const data = event.target.closest('[data-kind]')?.dataset;
    const at = time(event);
    if (!Number.isFinite(at)) return;
    const kind = data?.kind || 'audition';
    const edge = data?.edge || 'move';
    if (kind === 'section') {
      const section = sections.find((s) => s.id === data.id);
      if (!section) return;
      if (edge === 'move') { onPick(section.id); return; }
      drag = { pointer: event.pointerId, id: section.id, kind, edge, origin: at,
        start: section.bounds.start_s, end: section.bounds.end_s };
    } else {
      const length = range.end - range.start;
      const start = data ? range.start : clamp(at - length / 2, 0, duration - length);
      drag = { pointer: event.pointerId, kind, edge, origin: at, start, end: start + length };
      if (!data) onPassage(edge, drag.start, drag.end, 0);
    }
    area.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  area.addEventListener('pointermove', (event) => {
    if (drag?.pointer !== event.pointerId) return;
    const delta = time(event) - drag.origin;
    if (drag.kind === 'audition') onPassage(drag.edge, drag.start, drag.end, delta);
    else onSection(drag.id, drag.edge, drag[drag.edge] + delta);
  });

  function endDrag(event) { if (drag?.pointer === event.pointerId) drag = null; }
  area.addEventListener('pointerup', endDrag);
  area.addEventListener('lostpointercapture', endDrag);
  area.addEventListener('pointercancel', endDrag);

  draw();
  return {
    el,
    update(nextRange, nextSections, activeId) {
      range = nextRange;
      if (zoom && range && !drag && (range.start < zoom[0] || range.end > zoom[1])) zoom = passageWindow(range);
      sections = nextSections;
      active = activeId;
      draw();
    },
    overview(data) {
      if (!Array.isArray(data?.peaks) || !(data.bin_duration_s > 0) || data.basis !== 'original_sample_peak') return;
      peaks = data;
      drawWave();
      basis.textContent = `Original sample peaks across both channels · ${fmtNum(data.bin_duration_s, 2)} s per bin.`;
    },
  };
}
