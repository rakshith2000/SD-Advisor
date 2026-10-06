/* Tests for static/table-sort.js, run against a real DOM.
 *
 *     npm install --no-save jsdom     # once; nothing else is needed
 *     node tests/test_table_sort.js
 *
 * jsdom is deliberately NOT a committed dependency. The service ships no
 * JavaScript toolchain - one static file, served as-is - and adding a
 * package.json for a single test file would put 26 MB of node_modules and a
 * lockfile into a Python project that has neither. Install it when you change
 * the sorter, remove it afterwards.
 *
 * The property under test is that sorting is done on the data-* values and
 * not on the rendered text. The fixture is built so the two disagree: Age
 * renders "10 Days" and "2 Days", which order one way as strings and the
 * other way as minutes. A test that passed by reading the cells would get
 * the age assertions backwards.
 *
 * tests/test_board_markup.py covers the other half of the seam - that the
 * template emits the data-* attributes this file assumes - and that one runs
 * in the normal pytest suite with no extra install.
 */

'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  // A stack trace about a missing module reads as a broken test rather than
  // as an absent optional tool, and the next person would go looking for the
  // bug instead of running one command.
  console.error(
    '\n  jsdom is not installed, so these tests cannot run.\n\n' +
    '      npm install --no-save jsdom\n\n' +
    '  It is intentionally not committed - see the note at the top of this\n' +
    '  file. tests/test_board_markup.py covers the template side and needs\n' +
    '  nothing extra.\n');
  process.exit(2);
}

const SCRIPT = fs.readFileSync(
  path.join(__dirname, '..', 'web', 'static', 'table-sort.js'), 'utf8');

const DAY = 1440;

// Deliberately: text order and numeric order disagree on age and idle.
const ROWS = [
  { n: 'INC0003', score: 70, agent: 'asha rao', age: 10 * DAY, idle: 10 * DAY, action: 'Resolve' },
  { n: 'INC0001', score: 9, agent: 'Zoe Clark', age: 2 * DAY, idle: 2 * DAY, action: 'Escalate' },
  { n: 'INC0002', score: 45, agent: '', age: 21 * DAY, idle: 1 * DAY, action: 'Reassign' },
];

const HEADERS = [
  ['score', 'Score', 'num'],
  ['ticket', 'Ticket', 'text'],
  ['agent', 'Assigned to', 'text'],
  ['age', 'Age', 'num'],
  ['idle', 'Idle', 'num'],
  ['action', 'Suggested next step', 'text'],
];

function build(rows = ROWS) {
  const thead = HEADERS.map(([key, label, type]) =>
    `<th class="sortable" data-sort="${key}" data-type="${type}"` +
    ` data-label="${label}" aria-sort="none">` +
    `<button type="button">${label}<span class="sort-indicator"></span></button></th>`
  ).join('');

  const tbody = rows.map(r =>
    `<tr data-score="${r.score}" data-ticket="${r.n}" data-agent="${r.agent}"` +
    ` data-age="${r.age}" data-idle="${r.idle}" data-owner="Service Desk"` +
    ` data-action="${r.action}">` +
    // The rendered duration, which orders differently from the minutes above.
    `<td>${r.score}</td><td>${r.n}</td><td>${r.agent || 'Unassigned'}</td>` +
    `<td>${Math.floor(r.age / DAY)} Days</td>` +
    `<td>${Math.floor(r.idle / DAY)} Days</td><td>${r.action}</td></tr>`
  ).join('');

  return `<!DOCTYPE html><html><body>
    <span id="note" data-default="Highest attention score first">Highest attention score first</span>
    <a href="#" id="reset" hidden>back to the ranking</a>
    <table class="data" data-sortable="board" data-sort-note="note" data-sort-reset="reset">
      <thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>
  </body></html>`;
}

function mount(html, before) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'https://ata.test/board' });
  if (before) { before(dom); }
  dom.window.eval(SCRIPT);
  // A <script src> at the end of <body> runs while the document is still
  // parsing, so the browser fires this immediately afterwards and before any
  // user can click. JSDOM leaves readyState at 'loading' when the document is
  // built from a string, so the test releases it explicitly.
  if (dom.window.document.readyState === 'loading') {
    dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  }
  return dom;
}

function order(dom) {
  return Array.from(dom.window.document.querySelectorAll('tbody tr'))
    .map(r => r.dataset.ticket);
}

function load(html = build()) {
  const dom = mount(html);
  const doc = dom.window.document;
  return {
    dom,
    doc,
    table: doc.querySelector('table'),
    click(key) {
      doc.querySelector(`th[data-sort="${key}"] button`).dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }));
    },
    order() { return order(dom); },
    th(key) { return doc.querySelector(`th[data-sort="${key}"]`); },
    note() { return doc.getElementById('note').textContent; },
  };
}

const tests = {};
const test = (name, fn) => { tests[name] = fn; };

// ---------------------------------------------------------------------------
// ordering
// ---------------------------------------------------------------------------

test('the server order is left alone until a header is pressed', () => {
  assert.deepStrictEqual(load().order(), ['INC0003', 'INC0001', 'INC0002']);
});

test('a numeric column sorts descending on the first press', () => {
  const t = load();
  t.click('score');
  assert.deepStrictEqual(t.order(), ['INC0003', 'INC0002', 'INC0001']);
});

test('pressing the active column again reverses it', () => {
  const t = load();
  t.click('score');
  t.click('score');
  assert.deepStrictEqual(t.order(), ['INC0001', 'INC0002', 'INC0003']);
});

test('age sorts on minutes, not on the rendered duration', () => {
  // As text: "10 Days" < "2 Days" < "21 Days". As minutes: 2 < 10 < 21.
  const t = load();
  t.click('age');
  assert.deepStrictEqual(t.order(), ['INC0002', 'INC0003', 'INC0001'],
    'age appears to have been sorted as displayed text');
});

test('idle sorts on minutes too', () => {
  const t = load();
  t.click('idle');
  t.click('idle');
  assert.deepStrictEqual(t.order(), ['INC0002', 'INC0001', 'INC0003']);
});

test('a text column sorts ascending on the first press', () => {
  const t = load();
  t.click('agent');
  // '' then 'asha rao' then 'Zoe Clark' - case-insensitively.
  assert.deepStrictEqual(t.order(), ['INC0002', 'INC0003', 'INC0001']);
});

test('text comparison ignores case', () => {
  const t = load();
  t.click('agent');
  t.click('agent');
  assert.deepStrictEqual(t.order(), ['INC0001', 'INC0003', 'INC0002'],
    'lower-case names sorted after capitalised ones');
});

test('a row with no assignee sorts without throwing', () => {
  const t = load();
  t.click('agent');
  assert.strictEqual(t.order().length, 3);
});

test('a non-numeric value sorts as zero rather than scrambling the order', () => {
  const rows = [
    { n: 'INC0001', score: 'n/a', agent: 'A', age: DAY, idle: DAY, action: 'Resolve' },
    { n: 'INC0002', score: 50, agent: 'B', age: DAY, idle: DAY, action: 'Resolve' },
  ];
  const t = load(build(rows));
  t.click('score');
  assert.deepStrictEqual(t.order(), ['INC0002', 'INC0001']);
});

test('the sort is stable, so ties keep the server ranking', () => {
  const rows = [0, 1, 2, 3, 4].map(i => ({
    n: `INC000${i}`, score: 90 - i, agent: 'Same Person',
    age: (10 - i) * DAY, idle: DAY, action: 'Resolve',
  }));
  const t = load(build(rows));
  const before = t.order();
  t.click('agent');
  assert.deepStrictEqual(t.order(), before, 'ties were reshuffled');
  t.click('agent');
  assert.deepStrictEqual(t.order(), before, 'ties were reshuffled on reverse');
});

test('an empty table does not throw', () => {
  assert.strictEqual(load(build([])).order().length, 0);
});

// ---------------------------------------------------------------------------
// no reload
// ---------------------------------------------------------------------------

test('sorting navigates nowhere', () => {
  const t = load();
  const before = t.dom.window.location.href;
  t.click('age');
  t.click('agent');
  assert.strictEqual(t.dom.window.location.href, before);
});

test('the header button is a button, not a link', () => {
  const t = load();
  const btn = t.th('age').querySelector('button');
  assert.strictEqual(btn.tagName, 'BUTTON');
  assert.strictEqual(btn.getAttribute('type'), 'button');
  assert.strictEqual(t.th('age').querySelector('a'), null);
});

test('the same rows are reused rather than rebuilt', () => {
  // Re-rendering would discard anything the browser is holding on a row -
  // text selection, focus, an open context menu.
  const t = load();
  const row = t.doc.querySelector('tbody tr');
  t.click('age');
  assert.ok(t.doc.body.contains(row), 'rows were replaced instead of moved');
});

// ---------------------------------------------------------------------------
// what the header says
// ---------------------------------------------------------------------------

test('only the pressed column is marked', () => {
  const t = load();
  t.click('age');
  assert.ok(t.th('age').classList.contains('is-sorted'));
  assert.ok(!t.th('score').classList.contains('is-sorted'));
});

test('aria-sort reports the direction', () => {
  const t = load();
  t.click('age');
  assert.strictEqual(t.th('age').getAttribute('aria-sort'), 'descending');
  t.click('age');
  assert.strictEqual(t.th('age').getAttribute('aria-sort'), 'ascending');
  assert.strictEqual(t.th('score').getAttribute('aria-sort'), 'none');
});

test('the indicator follows the direction and clears when inactive', () => {
  const t = load();
  t.click('age');
  assert.strictEqual(t.th('age').querySelector('.sort-indicator').textContent, '▼');
  t.click('age');
  assert.strictEqual(t.th('age').querySelector('.sort-indicator').textContent, '▲');
  t.click('score');
  assert.strictEqual(t.th('age').querySelector('.sort-indicator').textContent, '');
});

test('the tooltip describes a number differently from a name', () => {
  const t = load();
  assert.match(t.th('age').querySelector('button').title, /highest first/);
  assert.match(t.th('agent').querySelector('button').title, /A to Z/);
});

test('the caption describes the current order', () => {
  const t = load();
  t.click('idle');
  assert.strictEqual(t.note(), 'Sorted by idle, highest first');
  t.click('agent');
  assert.strictEqual(t.note(), 'Sorted by assigned to, A to Z');
});

// ---------------------------------------------------------------------------
// back to the ranking
// ---------------------------------------------------------------------------

test('the reset link is hidden until the order has been changed', () => {
  const t = load();
  assert.strictEqual(t.doc.getElementById('reset').hidden, true);
  t.click('age');
  assert.strictEqual(t.doc.getElementById('reset').hidden, false);
});

test('reset restores the exact order the server sent', () => {
  const t = load();
  const original = t.order();
  t.click('age');
  t.click('agent');
  t.doc.getElementById('reset').dispatchEvent(
    new t.dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert.deepStrictEqual(t.order(), original);
  assert.strictEqual(t.note(), 'Highest attention score first');
  assert.strictEqual(t.doc.getElementById('reset').hidden, true);
  assert.ok(!t.th('age').classList.contains('is-sorted'));
});

// ---------------------------------------------------------------------------
// persistence across the page loads that filtering causes
// ---------------------------------------------------------------------------

test('the chosen order survives the reload that applying a filter causes', () => {
  const html = build();
  const first = load(html);
  first.click('idle');
  const stored = first.dom.window.localStorage.getItem('ata-sort-board');
  assert.strictEqual(stored, 'idle:desc');

  // A second page, as if the filter form had been submitted.
  const dom = mount(html, d => d.window.localStorage.setItem('ata-sort-board', stored));
  assert.deepStrictEqual(order(dom), ['INC0003', 'INC0001', 'INC0002']);
  assert.ok(dom.window.document.querySelector('th[data-sort="idle"]')
    .classList.contains('is-sorted'));
});

test('reset forgets the preference as well as undoing it', () => {
  const t = load();
  t.click('age');
  t.doc.getElementById('reset').dispatchEvent(
    new t.dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert.strictEqual(t.dom.window.localStorage.getItem('ata-sort-board'), null);
});

test('a stored column that no longer exists is ignored', () => {
  // Otherwise removing a column leaves a preference that sorts nothing and
  // marks no header, which reads as the sort being broken.
  const dom = mount(build(),
    d => d.window.localStorage.setItem('ata-sort-board', 'nosuchcolumn:desc'));
  assert.deepStrictEqual(order(dom), ['INC0003', 'INC0001', 'INC0002']);
});

test('a stored direction that is not asc or desc is ignored', () => {
  const dom = mount(build(),
    d => d.window.localStorage.setItem('ata-sort-board', 'age:sideways'));
  assert.deepStrictEqual(order(dom), ['INC0003', 'INC0001', 'INC0002']);
});

// ---------------------------------------------------------------------------
// the failure that would otherwise be invisible
// ---------------------------------------------------------------------------

test('a header with no matching row attribute is reported', () => {
  // A column that appears to sort and does not is invisible on screen, so it
  // has to be loud somewhere.
  const html = build().replace(/ data-age="\d+"/g, '');
  const warnings = [];
  mount(html, d => { d.window.console.warn = (...a) => warnings.push(a.join(' ')); });
  assert.ok(warnings.some(w => w.includes('data-age')),
    'a missing sort attribute was not reported');
});

test('a table without the opt-in attribute is left alone', () => {
  const dom = mount(build().replace('data-sortable="board"', ''));
  dom.window.document.querySelector('th[data-sort="age"] button')
    .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  assert.deepStrictEqual(order(dom), ['INC0003', 'INC0001', 'INC0002']);
});

// ---------------------------------------------------------------------------

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`  FAIL ${name}\n       ${err.message.split('\n').join('\n       ')}`);
  }
}
const total = Object.keys(tests).length;
console.log(`\n${total - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
