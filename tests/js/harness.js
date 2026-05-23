/**
 * JS test harness — load the SPA into jsdom and expose its top-level
 * functions for direct testing.
 *
 * Why this exists: a render-time exception (TDZ, undefined ref, calling
 * a typo'd function) in app.js leaves the panel blank and silent. Python
 * tests can't see it. This harness evaluates app.js in a jsdom context
 * so we can call renderAnalysis(...) etc. directly and assert that the
 * mount actually filled with children.
 *
 * The SPA expects a #app wrapper and a #view-mount. We set both up + a
 * minimal STATE so the renderers can run against fixtures without
 * needing a server.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.resolve(HERE, '..', '..', 'web', 'static', 'app.js');

/**
 * Build a jsdom and evaluate app.js inside it. Returns { window, dom }.
 * Top-level `function foo()` declarations become globals on window.
 */
export function loadApp({ html = '<!doctype html><html><body><div id="app"></div></body></html>' } = {}) {
  const dom = new JSDOM(html, {
    url: 'http://localhost:8000/',
    pretendToBeVisual: true,
    // 'dangerously' lets us inject a <script> tag with app.js so its
    // top-level `function foo()` declarations land on window — `eval`
    // inside the window context doesn't promote them in jsdom 25.
    runScripts: 'dangerously',
  });
  const { window } = dom;

  // Polyfills the SPA quietly assumes — jsdom omits a few real-browser APIs.
  if (!window.matchMedia) {
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  }
  if (!window.fetch) {
    // Default to a benign empty-success so any bootstrap fetch doesn't blow up.
    window.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
  }

  // Stub timers + listeners that would keep node:test alive forever. The
  // app's bootstrap kicks off setInterval polling for monitor/auth/etc;
  // we don't need any of that for unit testing the render functions, so
  // make these no-ops. Functions declared with `function` still land on
  // window — that's all we need for testing.
  window.setInterval = () => 0;
  window.setTimeout = (fn, _ms) => 0;  // never fire deferred work in tests
  const origAdd = window.document.addEventListener.bind(window.document);
  window.document.addEventListener = (event, handler, opts) => {
    // Skip the bootstrap's DOMContentLoaded — we test render fns directly
    if (event === 'DOMContentLoaded') return;
    return origAdd(event, handler, opts);
  };
  const origWindowAdd = window.addEventListener.bind(window);
  window.addEventListener = (event, handler, opts) => {
    if (event === 'DOMContentLoaded' || event === 'load') return;
    return origWindowAdd(event, handler, opts);
  };

  const code = fs.readFileSync(APP_JS, 'utf8');
  // Inject as a <script> tag — jsdom evaluates it the same way a browser
  // would, so top-level `function foo()` declarations become properties
  // on window. eval() doesn't do this promotion in jsdom 25.
  const script = window.document.createElement('script');
  script.textContent = code;
  try { window.document.body.appendChild(script); }
  catch (e) {
    if (process.env.DORE_TEST_DEBUG) console.error('script err:', e.message);
  }

  return { window, dom };
}

/**
 * Build a fresh #view mount node inside the document — destination for
 * a render call. Returns the mount element.
 */
export function makeMount(window, id = 'mount') {
  const el = window.document.createElement('div');
  el.id = id;
  window.document.body.appendChild(el);
  return el;
}
