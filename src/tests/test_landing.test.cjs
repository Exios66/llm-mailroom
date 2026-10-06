// Exercise the unmodified inline script; no server, npm packages, or real timers.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const html = readFileSync(path.join(__dirname, '../..', 'landing/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'Expected the standalone landing-page script');

// Minimal DOM surface used by the page. IDs and initial button/panel attributes
// come from the real HTML so missing elements fail instead of being invented.
class Element {
  constructor(attrs = '') {
    this.attrs = Object.fromEntries([...attrs.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
    this.hidden = /\bhidden\b/.test(attrs);
    this.dataset = {};
    this.style = {};
    this.innerHTML = '';
    this.textContent = '';
    this.children = [];
    this.listeners = {};
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); }
  fire(type, event = {}) {
    event.target ??= this;
    event.stopPropagation ??= () => { event.stopped = true; };
    for (const handler of this.listeners[type] ?? []) handler(event);
    return event;
  }
  closest(selector) { return selector === 'a' && this.tagName === 'a' ? this : null; }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  appendChild(child) { this.children.push(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  get firstChild() { return this.children[0]; }
}

function boot(options = {}) {
  const elements = new Map([...html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)].map(m => {
    const element = new Element(m[2]);
    element.tagName = m[1];
    return [m[3], element];
  }));
  const document = new Element();
  document.documentElement = new Element();
  document.getElementById = id => elements.get(id) ?? null;
  document.createElement = () => new Element();
  const selection = { ranges: ['previous selection'], removeAllRanges() { this.ranges = []; }, addRange(r) { this.ranges.push(r); } };
  document.createRange = () => ({ selectNodeContents(node) { this.node = node; } });
  const storage = new Map(options.theme ? [['mailroom-theme', options.theme]] : []);
  const localStorage = {
    getItem(key) { if (options.storageError) throw Error('storage denied'); return storage.get(key) ?? null; },
    setItem(key, value) { if (options.storageError) throw Error('storage denied'); storage.set(key, value); },
  };
  const clipboard = [];
  const navigator = options.clipboard === 'missing' ? {} : {
    clipboard: { writeText(text) {
      clipboard.push(text);
      return options.clipboard === 'denied' ? Promise.reject(Error('clipboard denied')) : Promise.resolve();
    } },
  };
  const timers = [], delays = [], random = [];
  const math = Object.create(Math);
  math.random = () => random.length ? random.shift() : 0.5;
  vm.runInNewContext(scripts[0][1], {
    document, localStorage, navigator, Math: math,
    getSelection: () => selection,
    matchMedia: query => ({ matches: query.includes('reduced-motion') ? !!options.reduce : !!options.dark }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); delays.push(ms); },
  }, { filename: 'landing/index.html', timeout: 1000 });
  const get = id => {
    assert.ok(elements.has(id), `Missing DOM element ${id}`);
    return elements.get(id);
  };
  async function tick() {
    assert.ok(timers.length, 'Simulation stopped scheduling work');
    timers.shift().fn();
    // Flush async deliver(), loop(), and clipboard promise continuations.
    for (let i = 0; i < 4; i++) await Promise.resolve();
  }
  function logs() { return get('log').children.map(line => line.innerHTML).join('\n'); }
  function bin(name) {
    const match = get('bins').innerHTML.match(new RegExp(`<span>${name}/</span><span class="n">(\\d+)</span>`));
    assert.ok(match, `Missing bin ${name}`);
    return Number(match[1]);
  }
  function enqueueDelivery({ route = 0.5, confidence = 0.5, judge = 0.5, hash = 0.5 } = {}) {
    // Calls: rescan, six ID digits, review draw, confidence, text size;
    // archive additionally draws field count, judge choice, eight hash digits.
    random.push(0.5, ...Array(6).fill(0.5), route, confidence, 0.5);
    if (route >= 0.2) random.push(0.5, judge, ...Array(8).fill(hash));
  }
  async function finishDelivery() {
    for (let i = 0; i < 25; i++) {
      await tick();
      if (get('mood').innerHTML.includes('idle · waiting')) return;
    }
    assert.fail('Delivery did not return to idle in 25 scheduled steps');
  }
  return { get, document, storage, clipboard, selection, timers, delays, random, tick, logs, bin, enqueueDelivery, finishDelivery };
}

for (const [theme, dark, expected] of [
  [undefined, false, 'dark'], [undefined, true, 'light'],
  ['light', true, 'dark'], ['dark', false, 'light'],
]) {
  test(`theme toggle respects saved=${theme} and system dark=${dark}`, () => {
    const page = boot({ theme, dark });
    assert.equal(page.document.documentElement.dataset.theme, theme);
    page.get('theme').fire('click');
    assert.equal(page.document.documentElement.dataset.theme, expected);
    assert.equal(page.storage.get('mailroom-theme'), expected);
    page.get('theme').fire('click');
    assert.equal(page.document.documentElement.dataset.theme, expected === 'dark' ? 'light' : 'dark');
  });
}

test('blocked storage still allows theme toggles and simulation startup', () => {
  const page = boot({ storageError: true });
  page.get('theme').fire('click');
  assert.equal(page.document.documentElement.dataset.theme, 'dark');
  assert.equal(page.timers.length, 1);
  assert.match(page.logs(), /watcher-started/);
});

test('menu toggles ARIA state and closes only for links, outside clicks, or Escape', () => {
  const page = boot(), button = page.get('menu-btn'), panel = page.get('menu-panel');
  const check = open => {
    assert.equal(panel.hidden, !open);
    assert.equal(button.attrs['aria-expanded'], String(open));
  };
  check(false);
  assert.equal(button.fire('click').stopped, true);
  check(true);
  page.document.fire('click', { target: panel });
  panel.fire('click');
  page.document.fire('keydown', { key: 'Enter' });
  check(true);
  button.fire('click');
  check(false);
  for (const close of [
    () => panel.fire('click', { target: { closest: () => ({ tagName: 'a' }) } }),
    () => page.document.fire('click', { target: new Element() }),
    () => page.document.fire('keydown', { key: 'Escape' }),
  ]) {
    button.fire('click');
    check(true);
    close();
    check(false);
  }
});

test('copy sends the exact two commands and resets its success feedback', async () => {
  const page = boot(), button = page.get('copy');
  button.fire('click');
  await Promise.resolve();
  assert.deepEqual(page.clipboard, ['pip install -e ".[dev]"\nPYTHONPATH=src python -m api.main']);
  assert.equal(button.attrs['aria-label'], 'Copied');
  assert.equal(button.style.borderColor, 'var(--ok)');
  const reset = page.timers.find(timer => timer.ms === 1500);
  assert.ok(reset);
  reset.fn();
  assert.equal(button.attrs['aria-label'], 'Copy install commands');
  assert.equal(button.style.borderColor, '');
});

for (const clipboard of ['missing', 'denied']) {
  test(`copy selects install text when clipboard is ${clipboard}`, async () => {
    const page = boot({ clipboard });
    page.get('copy').fire('click');
    await Promise.resolve();
    assert.equal(page.selection.ranges.length, 1);
    assert.equal(page.selection.ranges[0].node, page.get('install-cmd'));
    assert.equal(page.get('copy').attrs['aria-label'], 'Copy install commands');
    assert.equal(page.timers.length, 1); // no false success timer
  });
}

test('startup renders baseline counts, verifies the simulated chain, and waits for mail', () => {
  const page = boot();
  assert.deepEqual(['inbox', 'processing', 'review', 'failed', 'archive'].map(page.bin), [0, 0, 2, 0, 37]);
  assert.equal(Number(page.get('s-chain').textContent), 412);
  assert.equal(Number(page.get('s-rev').textContent), 2);
  assert.equal(Number(page.get('s-arch').textContent), 37);
  assert.match(page.logs(), /audit-chain-verified/);
  assert.doesNotMatch(page.logs(), /inbox-received/);
  assert.match(page.get('current').innerHTML, /width:0/);
  assert.equal(page.timers[0].ms, 5750);
});

test('delivery transfers inbox to processing and renders unknown then known confidence', async () => {
  const page = boot();
  page.enqueueDelivery({ confidence: 0 });
  await page.tick();
  assert.equal(page.bin('inbox'), 1);
  assert.equal(page.bin('processing'), 0);
  await page.tick();
  assert.equal(page.bin('inbox'), 0);
  assert.equal(page.bin('processing'), 1);
  assert.match(page.get('current').innerHTML, /sample_msa\.txt/);
  assert.match(page.get('current').innerHTML, /width:0%/);
  assert.match(page.get('flow').innerHTML, /class="node on">intake/);
  await page.tick();
  await page.tick();
  assert.match(page.get('current').innerHTML, /0\.97/);
  assert.match(page.get('current').innerHTML, /background:var\(--ok\)/);
  await page.finishDelivery();
  assert.match(page.get('current').innerHTML, /width:0"/);
  assert.doesNotMatch(page.get('flow').innerHTML, /class="node (on|done|review)"/);
});

for (const [route, confidence] of [[0, 0], [0.199999, 0.999999]]) {
  test(`review route at draws ${route}/${confidence} leaves archive and audit counts unchanged`, async () => {
    const page = boot();
    page.enqueueDelivery({ route, confidence });
    for (let i = 0; i < 5; i++) await page.tick();
    assert.match(page.get('flow').innerHTML, /class="node review">extract/);
    assert.match(page.get('current').innerHTML, /background:var\(--warn\)/);
    assert.match(page.logs(), confidence === 0 ? /0\.89/ : /0\.96/);
    await page.finishDelivery();
    assert.deepEqual(['inbox', 'processing', 'review', 'archive', 'contract'].map(page.bin), [0, 0, 3, 37, 14]);
    assert.equal(Number(page.get('s-rev').textContent), 3);
    assert.equal(Number(page.get('s-chain').textContent), 412);
    assert.match(page.logs(), /route-for-review/);
    assert.doesNotMatch(page.logs(), /extract-fields|write-catalog|archive-document|judge-verify/);
  });
}

for (const [judge, judged] of [[0.299999, true], [0.3, false]]) {
  test(`archive route includes judge only below 0.3 (draw=${judge})`, async () => {
    const page = boot();
    page.enqueueDelivery({ route: 0.2, confidence: 0, judge, hash: 0.25 });
    await page.finishDelivery();
    assert.deepEqual(['inbox', 'processing', 'review', 'archive', 'contract'].map(page.bin), [0, 0, 2, 38, 15]);
    assert.equal(Number(page.get('s-chain').textContent), 413);
    assert.equal(Number(page.get('s-arch').textContent), 38);
    assert.equal(page.logs().includes('judge-verify'), judged);
    assert.match(page.logs(), /extract-fields.*contracts_specialist/);
    assert.match(page.logs(), /write-catalog/);
    assert.match(page.logs(), /archive-document.*44444444….*9c1e40aa….*sealed/);
  });
}

test('successive deliveries cycle samples, update each class, link hashes, and bound the log', async () => {
  const page = boot();
  const samples = [
    ['sample_msa.txt', 'contracts_specialist'], ['atticus_03.pdf', 'contracts_specialist'],
    ['maud_merger_02.pdf', 'merger_agreement_specialist'], ['fnol_claim_0142.pdf', 'insurance_claims_specialist'],
    ['board_resolution_q3.pdf', 'corporate_records_specialist'], ['enron_memo_118.eml', 'correspondence_specialist'],
    ['denial_letter_0219.pdf', 'insurance_claims_specialist'], ['nda_mutual_v2.pdf', 'contracts_specialist'],
    ['bylaws_amendment.pdf', 'corporate_records_specialist'], ['sample_msa.txt', 'contracts_specialist'],
  ];
  for (const [index, [filename, specialist]] of samples.entries()) {
    page.enqueueDelivery({ hash: index === 0 ? 0.25 : 0.5 });
    await page.finishDelivery();
    assert.ok(page.logs().includes(filename));
    assert.ok(page.logs().includes(specialist));
    assert.ok(page.get('log').children.length <= 14);
    if (index === 1) assert.match(page.logs(), /archive-document.*88888888….*44444444…/);
  }
  assert.equal(page.get('log').children.length, 14);
  assert.doesNotMatch(page.logs(), /watcher-started/);
  assert.deepEqual(['contract', 'merger_agreement', 'corporate_record', 'correspondence', 'insurance_claim'].map(page.bin), [18, 4, 7, 10, 8]);
  assert.equal(page.bin('archive'), 47);
  assert.equal(Number(page.get('s-chain').textContent), 422);
});

test('reduced motion caps delays while preserving successful delivery behavior', async () => {
  const page = boot({ reduce: true });
  page.enqueueDelivery({ judge: 0 });
  await page.finishDelivery();
  assert.ok(page.delays.length > 5);
  assert.ok(page.delays.every(ms => ms <= 400));
  assert.equal(page.bin('archive'), 38);
  assert.equal(Number(page.get('s-chain').textContent), 413);
});

test('idle rescan logs a sweep before the next delivery', async () => {
  const page = boot();
  page.random.push(0.249999);
  await page.tick();
  assert.match(page.logs(), /rescan-sweep/);
  assert.ok(page.logs().indexOf('rescan-sweep') < page.logs().indexOf('inbox-received'));
});
