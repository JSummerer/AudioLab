// The desktop launcher gives this window a secret in its initial fragment. The server
// and bridge both check it. Keep it for a reload in this tab, and out of routed URLs.
const KEY = 'audiolab.desktop';
const incoming = globalThis.location?.hash?.match(/^#desktop=([A-Za-z0-9_-]{43})$/)?.[1];
let secret = incoming || '';
if (incoming) {
  history.replaceState(history.state, '', location.pathname + location.search);
  try { sessionStorage.setItem(KEY, incoming); } catch { /* this page only */ }
} else {
  try { secret = sessionStorage.getItem(KEY) || ''; } catch { /* ordinary browser or storage unavailable */ }
}

export const nativeWindow = () => !!secret;
export const nativeSecret = () => secret;

function picker() {
  return globalThis.window?.pywebview?.api;
}

export async function chooseNative(kind) {
  if (!nativeWindow()) return null;
  if (!picker()?.choose) {
    throw new Error('The file picker is unavailable. Try reopening AudioLab.');
  }
  const chosen = await picker().choose(secret, kind);
  if (chosen?.error) throw new Error(chosen.error);
  return chosen;
}

/** Optional metadata of the last explicit single-file selection, with no path access in JS. */
export async function nativeDuration(selection) {
  return picker()?.duration?.(secret, selection);
}
