// The persistent draft contains editor inputs only. Saved settings always come from the Library.
import { canonicalEq } from './eq.js';

export function snapshotDraft(draft, graphs) {
  const savedGraphs = {};
  for (const id of ['global', ...draft.sections.map((part) => part.id)]) {
    const graph = graphs.get(id);
    const snapshot = graph?.snapshot({ history: false }) || draft.graphs.get(id)?.snapshot;
    if (!snapshot) continue;
    const { bands, bypassed, text, textOpen, numbers } = snapshot;
    savedGraphs[id] = { bands, bypassed, text, textOpen, numbers,
      invalid: graph ? !!graph.read().errors.length : draft.graphs.get(id).invalid };
  }
  return {
    from: draft.from, length: draft.length, scope: draft.scope, values: { ...draft.values },
    sections: draft.sections.map(({ id, from, to, fade }) => ({ id, from, to, fade })), graphs: savedGraphs,
  };
}

/** Rebuild effective EQ and reset baselines from the editor snapshot and the version's own saved sections. */
export function restoreDraft(draft, data) {
  if (!data) return;
  const savedSections = draft.sections;
  const bandsFor = (id, fallback) => {
    const graph = data.graphs[id];
    if (!graph) return fallback;
    return graph.bypassed ? [] : canonicalEq(graph.bands);
  };
  Object.assign(draft, { from: data.from, length: data.length, scope: data.scope, values: { ...data.values },
    bands: bandsFor('global', draft.bands) });
  draft.sections = data.sections.map((s) => {
    const savedBands = savedSections[Number(s.id)]?.savedBands || [];
    return { ...s, savedBands, bands: bandsFor(s.id, savedBands) };
  });
  draft.nextId = Math.max(savedSections.length, ...draft.sections.map((part) => Number(part.id) + 1));
  for (const [id, { invalid, ...snapshot }] of Object.entries(data.graphs)) draft.graphs.set(id, { snapshot, invalid });
}

/** Serialize changes against the last acknowledged revision. A stale editor never overwrites a newer draft. */
export function draftWriter(record, send) {
  let pending;
  let running;
  let timer;
  const writer = {
    revision: record.revision, message: record.data ? 'Draft restored.' : '', notify: null, failed: false,
    queue(data) {
      pending = data;
      clearTimeout(timer);
      status('Saving draft…');
      timer = setTimeout(() => writer.flush(), 350);
    },
    async flush() {
      clearTimeout(timer);
      if (running) return running;
      if (pending === undefined) return !writer.failed;
      running = drain();
      try { return await running; } finally { running = null; }
    },
  };
  function status(message, failed = false) {
    writer.message = message;
    writer.failed = failed;
    writer.notify?.();
  }
  async function drain() {
    while (pending !== undefined) {
      const data = pending;
      pending = undefined;
      try {
        const { revision } = await send({ basis: record.basis, revision: writer.revision, data });
        writer.revision = revision;
      } catch (error) {
        if (pending === undefined) pending = data;
        status(`Draft not saved. ${error.message}`, true);
        return false;
      }
    }
    status('Draft saved.');
    return true;
  }
  return writer;
}
