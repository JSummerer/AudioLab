// HTTP client for the AudioLab server (contract: docs/API.md).

const TOKEN_KEY = 'audiolab.token';

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable: token lives for this page only */ memoryToken = token; }
}
let memoryToken = '';
/** The access token in use (stored or for this page only), or ''. */
export const token = () => getToken() || memoryToken;

/** URL for places that cannot send headers (<audio>, <img>, EventSource, links). */
export function url(path, params = null) {
  const u = new URL(path, location.origin);
  if (params) for (const [k, v] of Object.entries(params)) if (v != null) u.searchParams.set(k, v);
  const t = token();
  if (t) u.searchParams.set('token', t);
  return u.pathname + u.search;
}

export class ApiError extends Error {
  /** `hints` and `fix` come from AudioLabError responses ({detail, hints, fix}). */
  constructor(message, status = 0, data = null) {
    super(message);
    this.status = status;
    this.hints = Array.isArray(data?.hints) ? data.hints : [];
    this.fix = typeof data?.fix === 'string' ? data.fix : null;
  }
}

let authHandler = null;
export function onAuthRequired(fn) { authHandler = fn; }

function detailMessage(data) {
  if (!data) return '';
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || String(d)).join('; ');
  if (data.detail && typeof data.detail === 'object' && data.detail.message) return data.detail.message;
  return '';
}

export async function api(path, { method = 'GET', json, form, signal, text: asText = false } = {}) {
  const opts = { method, signal, headers: {} };
  const t = token();
  if (t) opts.headers['X-AudioLab-Token'] = t;
  if (json !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(json);
  } else if (form) {
    opts.body = form;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('Can’t reach the app. Make sure it is still running.', 0);
  }
  if (res.status === 401) {
    authHandler?.();
    throw new ApiError('This server needs an access token.', 401);
  }
  const text = await res.text();
  if (asText && res.ok) return text;
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) throw new ApiError(detailMessage(data) || `Something went wrong (error ${res.status}).`, res.status, data);
  return data;
}

/** Multipart POST with upload progress (fetch can't report upload progress). */
export function upload(path, form, onProgress, signal = null) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Upload stopped', 'AbortError')); return; }
    const xhr = new XMLHttpRequest();
    signal?.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.open('POST', path);
    const t = token();
    if (t) xhr.setRequestHeader('X-AudioLab-Token', t);
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    });
    xhr.addEventListener('load', () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { data = null; }
      if (xhr.status === 401) { authHandler?.(); reject(new ApiError('This server needs an access token.', 401)); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(detailMessage(data) || `Upload failed (error ${xhr.status}).`, xhr.status, data));
    });
    xhr.addEventListener('error', () => reject(new ApiError('Upload failed — the app could not be reached.', 0)));
    xhr.addEventListener('abort', () => reject(new DOMException('Upload stopped', 'AbortError')));
    xhr.send(form);
  });
}

export const mediaUrl = (jobId, key) =>
  url(`/api/jobs/${encodeURIComponent(jobId)}/media/${encodeURIComponent(key)}`);

export const fileUrl = (jobId, fileId) =>
  url(`/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(fileId)}`);

export const zipUrl = (jobId) => url(`/api/jobs/${encodeURIComponent(jobId)}/zip`);
