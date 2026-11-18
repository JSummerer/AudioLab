// Render the production Settings module's AI and Help sections with a finite DOM and a
// controlled API. These verify states, requests and guards, not browser layout or native delivery.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fmtBytes } from '../../audiolab/web/js/dom.js';

let activeDocument = null; // the fake document of the latest setup(), for focus()

export class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag;
    Object.assign(this, attrs);
    this.children = [];
    this.append(...children);
  }
  append(...nodes) {
    for (const n of nodes.flat(Infinity)) {
      if (n == null || n === false) continue;
      if (n instanceof Element) n.parent = this;
      this.children.push(n);
    }
  }
  replaceChildren(...nodes) {
    if (activeDocument?.activeElement !== this && this.contains(activeDocument?.activeElement)) {
      activeDocument.activeElement = activeDocument.body;
    }
    this.children = [];
    this.append(...nodes);
  }
  closest(selector) {
    const matches = selector === '[data-setting], [id]' ? this.dataset?.setting || this.id : this.tag === selector;
    return matches ? this : this.parent?.closest(selector);
  }
  scrollIntoView() {}
  addEventListener() {}
  setAttribute(k, v) { this[k] = v; }
  removeAttribute(k) { delete this[k]; }
  get classList() {
    return { contains: (s) => [this.class].flat().includes(s), add() {}, remove() {} };
  }
  querySelector(selector) {
    const matches = (n) => {
      if (selector.startsWith('[data-spot=')) return n.dataset?.spot === selector.split('"')[1];
      if (selector.startsWith('[data-autofocus]')) return n.dataset?.autofocus != null && !n.disabled;
      if (selector.startsWith('button:not')) return !n.disabled && ['button', 'select', 'input', 'a'].includes(n.tag);
      return false;
    };
    for (const n of this.children.filter((c) => c instanceof Element)) {
      const found = matches(n) ? n : n.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  contains(node) { return node === this || this.children.some((c) => c instanceof Element && c.contains(node)); }
  focus() { if (activeDocument) activeDocument.activeElement = this; }
  get childElementCount() { return this.children.filter((c) => c instanceof Element).length; }
}

export function find(root, id) {
  if (root?.id === id) return root;
  for (const child of root?.children || []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

export function text(root) {
  return root instanceof Element ? root.children.map(text).join(' ') : String(root);
}

/** `health` becomes store.health; `settings` store.settings. `nextHealth` (settable later) is what loadHealth() loads. */
export function setup(health, { settings = {}, crlf = false } = {}) {
  const body = new Element('section');
  const help = new Element('section');
  const ai = new Element('section');
  body.append(help, ai);
  const requests = [];
  const errors = [];
  const messages = [];
  const saved = [];
  const scheduled = [];
  // Like a browser: removing the focused element leaves focus on the page body.
  const document = { activeElement: null, body: new Element('body'), getElementById: (id) => find(body, id) };
  activeDocument = document;
  const context = {
    store: { health, settings, jobs: new Map() },
    ENHANCE_PRESETS: [], on() {}, isFinished: () => false,
    settingsPath: (k) => '#/settings/' + k, shortFolder: (s) => s,
    navigator: { platform: 'MacIntel' },
    segmented: (props) => new Element('div', props), chips: (props) => new Element('div', props),
    nextHealth: null,
    h: (tag, attrs, ...children) => new Element(tag, attrs || {}, children),
    append: (node, children) => node.append(children),
    clear: (node) => {
      if (node !== document.activeElement && node.contains(document.activeElement)) document.activeElement = document.body;
      node.children = [];
      return node;
    },
    led: (state) => new Element('span', { led: state }),
    icon: () => null,
    fmtBytes, deviceLabel: (device) => device, appName: () => 'AudioLab',
    switchControl: (props) => new Element('input', { type: 'checkbox', role: 'switch', ...props }),
    callout: (level, title, message) => new Element('div', { level }, [title, message]),
    saveSettings: async (patch) => { saved.push(patch); Object.assign(context.store.settings, patch); },
    document,
    nativeWindow: () => false,
    IS_MAC: false,
    api: async (path, options) => { requests.push({ path, ...options }); },
    toast: (message) => messages.push(message), toastError: (error) => errors.push(error),
    loadHealth: async () => { if (context.nextHealth) context.store.health = context.nextHealth; },
    confirmDialog: async () => true,
    setTimeout: (callback, delay) => { scheduled.push({ callback, delay }); return scheduled.length; },
    clearTimeout: () => {},
    body, help, ai,
  };
  const path = new URL('../../audiolab/web/js/views/settings.js', import.meta.url);
  const original = readFileSync(path, 'utf8');
  const checkout = crlf ? original.replace(/\r?\n/g, '\r\n') : original;
  const source = checkout.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
  vm.runInNewContext(`${source}
refs = { sections: { help: { body: help }, "ai-addon": { body: ai } }, live: { textContent: '' } };
settingsView.el = { querySelector: () => null };
visible = true;
renderHelp(); renderAi();`, context, { filename: path.pathname });
  return {
    body, requests, errors, messages, saved, context, scheduled,
    find: (id) => find(body, id),
    text: () => text(body),
    /** Replace Health (whole object) and redraw, as a 'health' event would. */
    render: (next) => { context.store.health = next; vm.runInNewContext('renderHelp(); renderAi()', context); },
    run: (code) => vm.runInNewContext(code, context),
  };
}
