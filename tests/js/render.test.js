/**
 * Render smoke tests — every view renderer must:
 *   - run without throwing (catches TDZ, undefined refs, typos)
 *   - actually fill the mount with > 0 children
 *
 * The original bug that motivated this file: a TDZ error in renderAnalysis
 * (calling `snapBody.append()` six lines BEFORE `const snapBody = ...`)
 * silently blanked the analyze view. Python tests passed; the regression
 * shipped. This file would have caught it on the first save.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadApp, makeMount } from './harness.js';
import {
  analysisFixture, sanctionsFixture, redemptionFixture, supplyFixture,
  marketFixture,
  simulatorStateFixture, simulatorPredictionFixture,
  simulatorCalibrationFixture,
} from './fixtures.js';

test('renderAnalysis renders a non-empty panel for a complete fixture', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  // Signature: renderAnalysis(a, elapsed, mount, computedAt, onRefresh)
  assert.doesNotThrow(() =>
    window.renderAnalysis(analysisFixture(), null, mount, Date.now() / 1000, () => {})
  );
  assert.ok(mount.children.length > 0, 'mount should have rendered children');
  // Snapshot panel + backing-strip should be there
  assert.ok(mount.querySelector('.backing-strip'), 'backing-model strip missing');
});

test('renderAnalysis renders correctly when no attestation exists (DAI-style)', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  const fixture = analysisFixture({ attestation: null, metrics: null,
    backing_model: 'crypto_collateral',
    protocol_url: 'https://makerburn.com' });
  assert.doesNotThrow(() =>
    window.renderAnalysis(fixture, null, mount, Date.now() / 1000, () => {})
  );
  assert.ok(mount.children.length > 0);
});

test('renderAnalysis renders with augmentations (AI context cards)', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  const fixture = analysisFixture({
    augmentations: [{
      surface: 'attestation',
      reason: 'no-fiat-attestation-by-design',
      text: 'Crypto-collateralized — see protocol dashboard.',
      citations: ['https://makerburn.com'],
      confidence: 'training-data-only',
      backing_model: 'crypto_collateral',
    }],
    backing_model: 'crypto_collateral',
  });
  assert.doesNotThrow(() =>
    window.renderAnalysis(fixture, null, mount, Date.now() / 1000, () => {})
  );
  assert.ok(mount.querySelector('.aug-card'), 'AI context card should render');
  assert.ok(mount.querySelector('.aug-cite'), 'cited URL should render as link');
});

test('renderAnalysis handles partial supply (some chains failed)', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  const fixture = analysisFixture({
    supply: {
      ...supplyFixture(),
      complete: false, chains_read: 3, chains_expected: 5,
      failed_chains: ['polygon', 'avalanche'],
      warnings: ['USDC: PARTIAL TOTAL — 3/5 chains read.'],
    },
  });
  assert.doesNotThrow(() =>
    window.renderAnalysis(fixture, null, mount, Date.now() / 1000, () => {})
  );
});

test('renderSanctions renders without throwing', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  // Signature: renderSanctions(s, elapsed, mount, computedAt, onRefresh)
  assert.doesNotThrow(() =>
    window.renderSanctions(sanctionsFixture(), null, mount, Date.now() / 1000, () => {})
  );
  assert.ok(mount.children.length > 0);
  assert.ok(mount.querySelector('.backing-strip'), 'backing strip missing on sanctions');
});

test('renderRedemption renders without throwing', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  assert.doesNotThrow(() =>
    window.renderRedemption(redemptionFixture(), null, mount, Date.now() / 1000, () => {})
  );
  assert.ok(mount.children.length > 0);
  assert.ok(mount.querySelector('.backing-strip'), 'backing strip missing on redemption');
});

test('renderMarket renders the freshness footer when validated_at is present', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  assert.doesNotThrow(() =>
    window.renderMarket(marketFixture(), mount)
  );
  const footer = mount.querySelector('.mkt-foot-fresh');
  assert.ok(footer, '.mkt-foot-fresh should render when freshness present');
  const text = footer.textContent || '';
  assert.match(text, /Aggregated from extractions performed between/);
  assert.match(text, /Last validated against issuer pages/);
});

test('renderMarket skips the freshness footer when freshness block is empty', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  const fixture = marketFixture({
    freshness: { earliest_extracted_at: '', latest_extracted_at: '',
      latest_validated_at: '' },
  });
  assert.doesNotThrow(() => window.renderMarket(fixture, mount));
  assert.equal(mount.querySelector('.mkt-foot-fresh'), null,
    'footer should be absent when no freshness data is available');
});

// Build a minimal timeline shape that matches what /api/simulator/
// timeline returns. The candle chart needs ticks + candles; the
// pistons need latest_prediction.
function simulatorTimelineFixture(symbol = 'USDC') {
  const now = Date.now();
  const ticks = [];
  const candles = [];
  for (let i = 5; i >= 0; i--) {
    const t = new Date(now - i * 5 * 60_000).toISOString();
    const v = 2 + Math.sin(i) * 1.5;
    ticks.push({ t, v, consensus_kind: 'agreed', max_disagreement_bps: 1 });
    candles.push({
      t_start: new Date(now - (i + 1) * 5 * 60_000).toISOString(),
      t_end: t, open: v - 0.4, high: v + 0.6,
      low: v - 0.6, close: v, count: 1,
    });
  }
  return {
    symbols: [symbol], bin_minutes: 5,
    tokens: [{
      symbol, ticks, candles,
      latest_prediction: {
        made_at: new Date(now).toISOString(),
        resolves_at: new Date(now + 60 * 60_000).toISOString(),
        point: 3.2,
        p50_low: 1.5, p50_high: 4.9,
        p80_low: -0.4, p80_high: 6.8,
        p95_low: -2.6, p95_high: 9.0,
        confidence_word: 'likely',
        horizon_minutes: 60,
        judge_synthesis: 'USDC sits inside the 50% band at +3.2bp.',
        judge_insight: 'Peg holds within stated cone.',
        judge_pitch: 'no action',
      },
    }],
  };
}

function buildSimData(overrides = {}) {
  return {
    state: simulatorStateFixture(overrides.stateOverrides || {}),
    preds: overrides.preds ||
      { predictions: [simulatorPredictionFixture()], count: 1 },
    calibration: overrides.calibration ||
      simulatorCalibrationFixture(),
    timeline: overrides.timeline || simulatorTimelineFixture(),
  };
}

test('renderSimulator renders pulley + SOTU + canvas + carousel + calibration', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  // Reset the persistent SIM_VIEW state so this test runs cold.
  if (window.SIM_VIEW) window.SIM_VIEW.selected = null;
  // Seed the selection so the canvas shows the test token.
  if (window.SIM_VIEW) window.SIM_VIEW.selected = new Set(['USDC']);
  assert.doesNotThrow(() =>
    window.renderSimulator(mount, buildSimData(), '')
  );
  // Pipeline pulley at the top.
  assert.ok(mount.querySelector('.sim-pulley'),
    'pipeline pulley should render');
  // SOTU strip with Brave quota.
  assert.ok(mount.querySelector('.sim-sotu'), 'SOTU strip should render');
  const sotuText = mount.querySelector('.sim-sotu').textContent;
  assert.match(sotuText, /BRAVE QUOTA/);
  assert.match(sotuText, /PEG SOURCES/);
  // Main canvas + chips + candle SVG.
  assert.ok(mount.querySelector('.sim-canvas'),
    'main canvas hero should render');
  assert.ok(mount.querySelector('.sim-chips'),
    'token chips ribbon should render');
  assert.ok(mount.querySelector('.sim-candle-svg'),
    'candle chart SVG should render');
  // Pistons present.
  assert.ok(mount.querySelector('.sim-pistons'),
    'forecast pistons column should render');
  // Judge carousel surfaces synthesis.
  assert.ok(mount.querySelector('.sim-carousel'),
    'judge carousel should render');
  assert.match(mount.querySelector('.sim-carousel').textContent,
    /3\.2bp/);
  // Calibration page present.
  assert.ok(mount.querySelector('.sim-calibration'),
    'calibration page should render');
});

test('renderSimulator handles empty archive gracefully', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) window.SIM_VIEW.selected = new Set();
  const emptyTimeline = { symbols: [], bin_minutes: 5, tokens: [] };
  const emptyCal = { count: 0, brier_mean: null, crps_mean: null,
    outcome_histogram: {}, reliability_bins: [],
    baseline_persistence_brier_mean: null,
    baseline_climatology_brier_mean: null };
  assert.doesNotThrow(() =>
    window.renderSimulator(mount, buildSimData({
      preds: { predictions: [], count: 0 },
      calibration: emptyCal,
      timeline: emptyTimeline,
    }), '')
  );
  assert.ok(mount.querySelector('.sim-empty'),
    'empty-state panel should render when no predictions');
  assert.ok(mount.querySelector('.sim-calibration-empty'),
    'empty calibration message should render');
});

test('renderSimulator carousel collapses when no judge output present', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) window.SIM_VIEW.selected = new Set(['USDC']);
  const tl = simulatorTimelineFixture();
  tl.tokens[0].latest_prediction.judge_synthesis = null;
  tl.tokens[0].latest_prediction.judge_insight = null;
  tl.tokens[0].latest_prediction.judge_pitch = null;
  assert.doesNotThrow(() =>
    window.renderSimulator(mount, buildSimData({ timeline: tl }), '')
  );
  // Empty-judge carousel renders its honest fallback message.
  assert.ok(mount.querySelector('.sim-carousel-empty'),
    'empty carousel should render its fallback when no judge text');
  const empty = mount.querySelector('.sim-carousel-empty');
  assert.match(empty.textContent,
    /has not synthesised|judge layer has not/);
});

test('renderRedemption handles crypto-collateralized (no tiers)', () => {
  const { window } = loadApp();
  const mount = makeMount(window);
  const fixture = redemptionFixture({
    attestation: null, tiers: [], liquid_reserves: 0,
    liquid_coverage: null, net_redemption_flow: null,
    backing_model: 'crypto_collateral',
    protocol_url: 'https://makerburn.com',
  });
  assert.doesNotThrow(() =>
    window.renderRedemption(fixture, null, mount, Date.now() / 1000, () => {})
  );
});
