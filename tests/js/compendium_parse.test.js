/**
 * Compendium HTML parse guard.
 *
 * The Compendium page ships its renderer inline inside compendium.html.
 * The render.test.js harness only loads app.js, so a syntax error in
 * the inline script silently leaves the page blank — pytest stays
 * green, the unit suite stays green, and the UI quietly dies.
 *
 * This test extracts every <script> block from compendium.html and
 * parses it with `new Function(...)`. A SyntaxError throws and fails
 * the suite. Caught the bug where an extra `),` slipped in after a
 * map() block-form refactor; would have caught it on save.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const COMPENDIUM = path.resolve(HERE, '..', '..', 'web', 'static', 'compendium.html');

test('compendium.html: every inline <script> block parses', () => {
  const html = fs.readFileSync(COMPENDIUM, 'utf8');
  // Naive but sufficient: capture the inner text of any <script> tag
  // that isn't a src= include. compendium.html is hand-authored and
  // doesn't use exotic attribute orderings.
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  const blocks = [];
  let m;
  while ((m = re.exec(html)) !== null) blocks.push(m[1]);
  assert.ok(blocks.length > 0, 'expected at least one inline <script> block');
  for (const code of blocks) {
    // new Function() runs the parser without executing the body.
    // Wrap in a try so we can attach the offending snippet to the
    // assertion message for easier triage.
    try {
      // eslint-disable-next-line no-new-func
      new Function(code);
    } catch (err) {
      const head = code.slice(0, 200).replace(/\n/g, '\\n');
      assert.fail(`script parse error: ${err.message}\nfirst chars: ${head}`);
    }
  }
});
