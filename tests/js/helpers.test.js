/**
 * Pure-function helper tests — small, fast, no DOM.
 *
 * Why: most of the rendering logic comes down to a few helpers
 * (consensusBadge, tokenStatus, backingModelStrip, augmentationCard,
 * sourceHealthBadge). Test their happy + edge cases directly so a typo
 * doesn't ride through unnoticed.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './harness.js';

test('tokenStatus: LIVE for clean read', () => {
  const { window } = loadApp();
  const st = window.tokenStatus({ warnings: [], complete: true });
  assert.equal(st.label, 'LIVE');
  assert.equal(st.tag, 'ok');
  assert.ok(st.tip.length > 0);
});

test('tokenStatus: WATCH for warnings', () => {
  const { window } = loadApp();
  const st = window.tokenStatus({ warnings: ['unverified deployment'], complete: true });
  assert.equal(st.label, 'WATCH');
  assert.equal(st.tag, 'watch');
});

test('tokenStatus: WATCH with partial-chains gets explicit failure list', () => {
  const { window } = loadApp();
  const st = window.tokenStatus({
    warnings: ['PARTIAL TOTAL'],
    complete: false, chains_read: 3, chains_expected: 5,
    failed_chains: ['polygon', 'avalanche'],
  });
  assert.equal(st.label, 'WATCH');
  assert.ok(st.tip.includes('polygon'));
  assert.ok(st.tip.includes('3/5'));
});

test('tokenStatus: ALERT for error', () => {
  const { window } = loadApp();
  const st = window.tokenStatus('error');
  assert.equal(st.label, 'ALERT');
  assert.equal(st.tag, 'alert');
});

test('tokenStatus: PENDING for no state', () => {
  const { window } = loadApp();
  const st = window.tokenStatus(null);
  assert.equal(st.label, 'PENDING');
  assert.equal(st.tag, 'idle');
});

test('consensusBadge: agreement gets green class', () => {
  const { window } = loadApp();
  const el = window.consensusBadge('2/2 agree');
  assert.match(el.className, /consensus-ok/);
  assert.equal(el.textContent, '2/2 agree');
});

test('consensusBadge: single source gets warn class', () => {
  const { window } = loadApp();
  const el = window.consensusBadge('1/2 single source');
  assert.match(el.className, /consensus-warn/);
});

test('consensusBadge: DISAGREEMENT gets bad class (loud)', () => {
  const { window } = loadApp();
  const el = window.consensusBadge('DISAGREEMENT (no majority)');
  assert.match(el.className, /consensus-bad/);
});

test('consensusBadge: empty input renders muted dash, not crash', () => {
  const { window } = loadApp();
  const el = window.consensusBadge('');
  assert.equal(el.textContent, '—');
});

test('backingModelStrip: known model renders badge + protocol link', () => {
  const { window } = loadApp();
  const strip = window.backingModelStrip({
    backing_model: 'crypto_collateral',
    protocol_url: 'https://makerburn.com',
  });
  assert.ok(strip.textContent.includes('crypto collateral'));
  const link = strip.querySelector('a.backing-link');
  assert.ok(link, 'should render the protocol link');
  assert.equal(link.getAttribute('href'), 'https://makerburn.com');
});

test('backingModelStrip: unknown model falls back gracefully', () => {
  const { window } = loadApp();
  // Should NOT throw on an unrecognized model — fall back to fiat_reserves.
  const strip = window.backingModelStrip({ backing_model: 'weird_new_thing' });
  assert.ok(strip.textContent.length > 0);
});

test('backingModelStrip: empty input falls back without crashing', () => {
  const { window } = loadApp();
  const strip = window.backingModelStrip({});
  assert.ok(strip.textContent.length > 0);
});

test('augmentationCard: empty ctx returns empty element, never crashes', () => {
  const { window } = loadApp();
  const card = window.augmentationCard(null);
  assert.ok(card);  // empty span, but real DOM node
  const card2 = window.augmentationCard({ text: '' });
  assert.ok(card2);
});

test('dataLineageBanner: strong corroboration gets green', () => {
  const { window } = loadApp();
  const supply = {
    complete: true, chains_read: 3, chains_expected: 3,
    per_chain: [
      { consensus: '2/2 agree' },
      { consensus: '2/2 agree' },
      { consensus: '2/2 agree' },
    ],
    failed_chains: [],
  };
  const el = window.dataLineageBanner(supply, null);
  assert.match(el.className, /lineage-strong/);
  assert.match(el.textContent, /3 chains read/);
  assert.match(el.textContent, /3 cross-RPC corroborated/);
});

test('dataLineageBanner: partial gets amber + PARTIAL kicker', () => {
  const { window } = loadApp();
  const supply = {
    complete: false, chains_read: 3, chains_expected: 5,
    per_chain: [], failed_chains: ['polygon', 'avalanche'],
  };
  const el = window.dataLineageBanner(supply, null);
  assert.match(el.className, /lineage-partial/);
  assert.match(el.textContent, /PARTIAL/);
  assert.match(el.textContent, /polygon/);
});

test('dataLineageBanner: DISAGREEMENT gets rose treatment (loud)', () => {
  const { window } = loadApp();
  const supply = {
    complete: true, chains_read: 2, chains_expected: 2,
    per_chain: [
      { consensus: '2/2 agree' },
      { consensus: 'DISAGREEMENT (no majority)' },
    ],
    failed_chains: [],
  };
  const el = window.dataLineageBanner(supply, null);
  assert.match(el.className, /lineage-disagree/);
  assert.match(el.textContent, /disagreement/i);
});

test('dataLineageBanner: includes metrics provenance when present', () => {
  const { window } = loadApp();
  const supply = {
    complete: true, chains_read: 1, chains_expected: 1, per_chain: [],
  };
  const metrics = { provenance: '1 issuer attestation · 5/5 chains read' };
  const el = window.dataLineageBanner(supply, metrics);
  assert.match(el.textContent, /metrics: 1 issuer attestation/);
});

test('theme: default returns dark when no localStorage', () => {
  const { window } = loadApp();
  // localStorage is per-jsdom; fresh = empty = dark
  assert.equal(window.getTheme(), 'dark');
});

test('theme: setTheme("light") persists + applies data-theme attr', () => {
  const { window } = loadApp();
  window.setTheme('light');
  assert.equal(window.getTheme(), 'light');
  assert.equal(
    window.document.documentElement.getAttribute('data-theme'),
    'light',
  );
});

test('theme: setTheme("dark") removes data-theme attr', () => {
  const { window } = loadApp();
  window.setTheme('light');
  window.setTheme('dark');
  assert.equal(window.getTheme(), 'dark');
  assert.equal(
    window.document.documentElement.getAttribute('data-theme'),
    null,
  );
});

test('aiBriefHero: renders headline + key points + relevant news', () => {
  const { window } = loadApp();
  const hero = window.aiBriefHero({
    surface: 'analyze', symbol: 'USDC',
    headline: 'USDC is fully backed and clean.',
    key_points: ['Coverage 100.1%', 'No SDN matches'],
    relevant_news: [{title: 'FSB guidance', url: 'https://fsb.org/x', source: 'fsb'}],
    citations: ['https://fsb.org/x'],
    generated_at: '2026-05-23T00:00:00+00:00',
  });
  assert.equal(hero.querySelector('.brief-badge').textContent, 'DORÉ BRIEF');
  assert.match(hero.querySelector('.brief-headline').textContent, /fully backed/);
  assert.equal(hero.querySelectorAll('.brief-point').length, 2);
  assert.equal(hero.querySelectorAll('.brief-news-row').length, 1);
  const link = hero.querySelector('.brief-news-title');
  assert.equal(link.getAttribute('href'), 'https://fsb.org/x');
});

test('aiBriefHero: empty brief returns empty span (no crash)', () => {
  const { window } = loadApp();
  assert.ok(window.aiBriefHero(null));
  assert.ok(window.aiBriefHero({}));
  assert.ok(window.aiBriefHero({ headline: '' }));
});

test('augmentationCard: full ctx renders text + citations as links', () => {
  const { window } = loadApp();
  const card = window.augmentationCard({
    surface: 'attestation',
    reason: 'no-fiat-attestation-by-design',
    text: 'Crypto-collateralized — see protocol dashboard.',
    citations: ['https://makerburn.com', 'https://docs.makerdao.com/'],
    confidence: 'training-data-only',
  });
  assert.ok(card.querySelector('.aug-tag'), 'should have AI CONTEXT badge');
  assert.ok(card.querySelector('.aug-body'), 'should have body text');
  const cites = card.querySelectorAll('.aug-cite');
  assert.equal(cites.length, 2, 'both citations should render as links');
});
