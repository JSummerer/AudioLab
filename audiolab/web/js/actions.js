// Job and system actions used by more than one view: cancel / retry / remove a
// job, clear finished jobs (asks first), copy a diagnostics report, and the "use my
// browser's sign-in" button. Each shows its own error toast and returns whether it worked.
import { h, icon } from './dom.js';
import { api } from './api.js';
import { store, upsertJob, removeJobs, isFinished } from './store.js';
import { toast, toastError, copyText, confirmDialog } from './ui.js';
import { go, settingsPath } from './router.js';

const jobPathApi = (id, what = '') => `/api/jobs/${encodeURIComponent(id)}${what ? `/${what}` : ''}`;

async function jobAction(job, what, done) {
  try {
    const updated = await api(jobPathApi(job.id, what), { method: 'POST' });
    if (updated?.id) upsertJob(updated);
    if (done) toast(done, { kind: 'info' });
    return true;
  } catch (e) {
    toastError(e);
    return false;
  }
}

export const cancelJob = (job, { quiet = false } = {}) => jobAction(job, 'cancel', quiet ? null : 'Cancelling…');
export const retryJob = (job, { quiet = false } = {}) => jobAction(job, 'retry', quiet ? null : 'Trying again.');

/** Removes the job from the list (never its output files). */
export async function removeJob(job) {
  try {
    await api(jobPathApi(job.id), { method: 'DELETE' });
    removeJobs([job.id]);
    return true;
  } catch (e) {
    toastError(e);
    return false;
  }
}

export async function clearFinished() {
  const ok = await confirmDialog({
    title: 'Clear finished jobs?',
    message: 'Done, failed and cancelled jobs leave this list, and Check reports go with them. '
      + 'Your files stay in the output folder and in the Library.',
    confirm: 'Clear finished',
  });
  if (!ok) return false;
  try {
    const res = await api('/api/jobs?scope=finished', { method: 'DELETE' });
    removeJobs(res?.deleted || [...store.jobs.values()].filter(isFinished).map((j) => j.id));
    toast('Cleared finished jobs. Your files are still in the output folder.', { kind: 'ok' });
    return true;
  } catch (e) {
    toastError(e);
    return false;
  }
}

/** Plain-text report (versions, tools, recent log, this job's journal) → clipboard. */
export function copyDiagnostics(jobId = null) {
  const path = jobId ? `/api/diagnostics?job=${encodeURIComponent(jobId)}` : '/api/diagnostics';
  return copyText(() => api(path, { text: true }).catch((e) => {
    throw e.status === 404 ? new Error('This version can’t make a report yet.') : e;
  }), { title: jobId ? 'Details for a bug report' : 'System report' });
}

/** For "sign in to confirm you're not a bot" / age limits: opens Settings at the
 * browser sign-in choice. Only on this computer (it reads a local browser's cookies). */
export function signInButton({ primary = true, small = false } = {}) {
  if (!store.health?.local) return null;
  return h('button', {
    type: 'button', class: ['btn', primary ? 'btn-primary' : 'btn-key', small && 'btn-sm'],
    onclick: () => go(settingsPath('cookies'), { focus: false }),
  }, icon('key', small ? 14 : 15), 'Use my browser’s sign-in…');
}
