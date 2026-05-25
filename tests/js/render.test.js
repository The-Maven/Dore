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

// v3 feed fixture — matches the /api/simulator/feed payload that
// powers the Bloomberg-class workspace. tokens carry brand + value
// + deltas + sparkline + latest_prediction; events carry the
// streaming wire rows; sources carry per-peg-source liveness.
function simulatorFeedFixture(overrides = {}) {
  const now = Date.now();
  const spark = [];
  for (let i = 9; i >= 0; i--) {
    spark.push({
      t: new Date(now - i * 60_000).toISOString(),
      v: 1 + Math.sin(i) * 1.5,
      ck: 'agreed',
    });
  }
  return {
    tokens: [{
      symbol: 'USDC',
      brand: { accent: '#4F9DFF', glow: '#4F9DFF66', name: 'Circle', branded: true },
      current_bps: 1.42,
      deltas: { d1m: 0.32, d5m: -0.15, d1h: 1.20, d24h: null, d7d: null },
      sparkline: spark,
      consensus: { kind: 'agreed', max_disagreement_bps: 1.2,
        sources: [{ name: 'coinbase', price: 1.0001 },
                  { name: 'kraken', price: 1.0002 }] },
      tick_count: 60,
      latest_prediction: {
        made_at: new Date(now).toISOString(),
        resolves_at: new Date(now + 60_000).toISOString(),
        point: 1.5,
        p50_low: 0.8, p50_high: 2.2,
        p80_low: 0.1, p80_high: 2.9,
        p95_low: -0.5, p95_high: 3.5,
        confidence_word: 'likely',
        horizon_minutes: 2,
        drivers: [],
        judge_synthesis: 'USDC sits inside the 50% band at +1.50bp.',
        judge_insight: 'Peg holds within stated cone.',
        judge_pitch: 'no action',
      },
      calibration: { count: 12, brier_mean: 0.18, crps_mean: 0.45,
        baseline_climatology_brier_mean: 0.25 },
      recent_resolutions: [
        // Oldest → newest. Spread of outcomes so the strip renders
        // every marker variant in tests.
        { resolved_at: new Date(now - 10*60_000).toISOString(),
          outcome_kind: 'inside_p50', actual_value: 1.2, point: 1.0,
          p50_low: 0.4, p50_high: 1.6, p80_low: -0.1, p80_high: 2.1,
          p95_low: -0.7, p95_high: 2.7, brier_score: 0.12 },
        { resolved_at: new Date(now - 9*60_000).toISOString(),
          outcome_kind: 'inside_p80', actual_value: 1.8, point: 1.1,
          p50_low: 0.5, p50_high: 1.7, p80_low: 0.0, p80_high: 2.2,
          p95_low: -0.6, p95_high: 2.8, brier_score: 0.18 },
        { resolved_at: new Date(now - 8*60_000).toISOString(),
          outcome_kind: 'inside_p95', actual_value: 2.5, point: 1.0,
          p50_low: 0.4, p50_high: 1.6, p80_low: -0.1, p80_high: 2.1,
          p95_low: -0.7, p95_high: 2.7, brier_score: 0.30 },
        { resolved_at: new Date(now - 7*60_000).toISOString(),
          outcome_kind: 'outside', actual_value: 3.5, point: 1.0,
          p50_low: 0.4, p50_high: 1.6, p80_low: -0.1, p80_high: 2.1,
          p95_low: -0.7, p95_high: 2.7, brier_score: 0.5 },
        { resolved_at: new Date(now - 6*60_000).toISOString(),
          outcome_kind: 'inside_p50', actual_value: 1.4, point: 1.3,
          p50_low: 0.7, p50_high: 1.9, p80_low: 0.2, p80_high: 2.4,
          p95_low: -0.4, p95_high: 3.0, brier_score: 0.10 },
        { resolved_at: new Date(now - 5*60_000).toISOString(),
          outcome_kind: 'inside_p50', actual_value: 1.5, point: 1.4,
          p50_low: 0.8, p50_high: 2.0, p80_low: 0.3, p80_high: 2.5,
          p95_low: -0.3, p95_high: 3.1, brier_score: 0.09 },
      ],
    }],
    events: [
      { kind: 'movement.ticker.cycle', ts: new Date(now).toISOString(),
        symbol: '', summary: 'tick cycle · 1 symbols', level: 'info' },
      { kind: 'movement.resolver.graded',
        ts: new Date(now - 30_000).toISOString(),
        symbol: 'USDC',
        summary: 'USDC resolved · inside_p50 · brier None',
        level: 'info' },
    ],
    sources: [
      { name: 'coinbase', fetched_at: now / 1000,
        consensus_hist: { agreed: 1 } },
      { name: 'kraken', fetched_at: now / 1000,
        consensus_hist: { agreed: 1 } },
    ],
    ticker: { running: true, last_tick_at: new Date(now).toISOString(),
              last_summary: null },
    config: { enabled: true, tick_interval_minutes: 1, horizon_minutes: 2,
              symbols: ['USDC'], kinds: ['peg_deviation'] },
    brave_quota: { day: '2026-05-25', calls: 4, cap: 200, remaining: 196 },
    calibration: simulatorCalibrationFixture(),
    computed_at: new Date(now).toISOString(),
    ...overrides,
  };
}

test('renderSimulator (v3) renders ribbon + status + workspace + wire + calibration', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) {
    window.SIM_VIEW.focused = 'USDC';
    window.SIM_VIEW.wireRows = [];
  }
  assert.doesNotThrow(() =>
    window.renderSimulator(mount, simulatorFeedFixture())
  );
  // Bloomberg ribbon (two rows: cells + peg tracks).
  assert.ok(mount.querySelector('.sim-ribbon'),
    'Bloomberg ribbon should render');
  assert.ok(mount.querySelector('.sim-ribbon-cell'),
    'ribbon should contain at least one token cell');
  assert.ok(mount.querySelector('.sim-pegtrack'),
    'peg-deviation track row should render');
  // Status strip.
  assert.ok(mount.querySelector('.sim-status'), 'status bar should render');
  const statusText = mount.querySelector('.sim-status').textContent;
  assert.match(statusText, /LIVE/);
  assert.match(statusText, /BRAVE QUOTA/);
  // Three-column workspace.
  assert.ok(mount.querySelector('.sim-workspace'), 'workspace should render');
  assert.ok(mount.querySelector('.sim-rail-left'),
    'left rail should render');
  assert.ok(mount.querySelector('.sim-hero'), 'hero pane should render');
  assert.ok(mount.querySelector('.sim-rail-right'),
    'right rail (the wire) should render');
  // Delta grid + sparkline + judge.
  assert.ok(mount.querySelector('.sim-delta-grid'),
    'delta grid should render');
  assert.ok(mount.querySelector('.sim-spark svg, .sim-rail-spark svg'),
    'sparkline SVG should render somewhere');
  // Judge synthesis surfaces.
  const heroText = mount.querySelector('.sim-hero').textContent;
  assert.match(heroText, /1\.50bp/);
  // THE WIRE has events.
  assert.ok(mount.querySelector('.sim-wire-rows'),
    'wire rows container should render');
  // Calibration page present.
  assert.ok(mount.querySelector('.sim-calibration'),
    'calibration page should render');
  // ── FORECAST CONE v4 — annotated. Pin every annotation. ─────────
  // "MODEL SAYS" plain-English callout (the single biggest readability
  // win per the research).
  const callout = mount.querySelector('.sim-cone-callout');
  assert.ok(callout, 'cone callout block should render');
  assert.match(callout.textContent, /MODEL SAYS/,
    'callout should carry the MODEL SAYS tag');
  assert.match(callout.textContent, /80%/,
    'callout should name the 80% band in prose');
  // Outside-the-cone caveat (NHC lesson — 5% land outside p95).
  assert.ok(mount.querySelector('.sim-cone-caveat'),
    'outside-the-cone caveat strip should render');
  // SVG endpoint labels — direct p50/p80/p95 reads at the right edge.
  const svgText = mount.querySelector('.sim-hero-svg').textContent;
  assert.match(svgText, /p50/,
    'cone should label the p50 endpoint directly');
  assert.match(svgText, /p80/,
    'cone should label the p80 endpoint directly');
  assert.match(svgText, /p95/,
    'cone should label the p95 endpoint directly');
  // NOW marker label
  assert.match(svgText, /NOW/,
    'NOW vertical line label should render');
  // Anchor label ($1.00 peg)
  assert.match(svgText, /\$1\.00/,
    'anchor line should be labelled with the $1.00 peg');
  // ── PER-TOKEN TRACK RECORD STRIP ────────────────────────────────
  // Lives between the cone and the AI Judge so the reader sees the
  // model's recent hit pattern alongside the current forecast.
  const track = mount.querySelector('.sim-track');
  assert.ok(track, 'per-token track record strip should render');
  assert.match(track.textContent, /TRACK RECORD/,
    'track record carries its tag');
  // Markers — one per resolution. Fixture has 6 rows.
  const markers = mount.querySelectorAll('.sim-track-mk');
  assert.equal(markers.length, 6,
    'one marker per resolution in fixture');
  // Outcome variety — fixture covers p50, p80, p95, out. The strip
  // must render at least the p50 + outside variants.
  assert.ok(mount.querySelector('.sim-track-mk-p50'),
    'inside_p50 outcomes render as bright-green markers');
  assert.ok(mount.querySelector('.sim-track-mk-out'),
    'outside outcomes render as red markers');
  // Hit-rate summary line
  assert.match(track.textContent, /resolved/,
    'summary line names the count');
  assert.match(track.textContent, /%/,
    'summary line carries percentages');
});


test('renderSimulator (v3) shows empty track record state when no resolutions', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) {
    window.SIM_VIEW.focused = 'USDC';
    window.SIM_VIEW.wireRows = [];
  }
  const feed = simulatorFeedFixture();
  feed.tokens[0].recent_resolutions = [];
  window.renderSimulator(mount, feed);
  const empty = mount.querySelector('.sim-track-empty');
  assert.ok(empty, 'empty-state track-record block should render');
  assert.match(empty.textContent, /no resolutions yet/,
    'empty state names the reason');
});

test('renderSimulator (v3) handles empty archive gracefully', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) {
    window.SIM_VIEW.focused = null;
    window.SIM_VIEW.wireRows = [];
  }
  assert.doesNotThrow(() =>
    window.renderSimulator(mount, simulatorFeedFixture({
      tokens: [], events: [], sources: [],
      calibration: { count: 0, brier_mean: null, crps_mean: null,
        outcome_histogram: {}, reliability_bins: [],
        baseline_persistence_brier_mean: null,
        baseline_climatology_brier_mean: null },
    }))
  );
  // Empty hero ("No data yet.")
  assert.ok(mount.querySelector('.sim-hero-empty'),
    'empty hero should render when no tokens');
  // Calibration page renders its empty state.
  assert.ok(mount.querySelector('.sim-calibration-empty'),
    'empty calibration message should render');
});

test('renderSimulator (v3) flashes ribbon cells on tick diff', () => {
  const { window } = loadApp();
  const mount = window.document.createElement('div');
  window.document.body.appendChild(mount);
  if (window.SIM_VIEW) {
    window.SIM_VIEW.focused = 'USDC';
    window.SIM_VIEW.wireRows = [];
  }
  // Initial render with +1.42bp.
  window.renderSimulator(mount, simulatorFeedFixture());
  // Simulate an SSE tick with a larger value.
  window.applySimTick(mount, {
    tokens: [{
      symbol: 'USDC',
      current_bps: 2.5,
      deltas: { d1m: 1.08, d5m: 1.0, d1h: 1.5, d24h: null, d7d: null },
      consensus: { kind: 'agreed' },
      brand: { accent: '#4F9DFF', name: 'Circle' },
    }],
    events: [],
    brave_quota: { calls: 5, cap: 200 },
    ticker_last_tick_at: new Date().toISOString(),
  });
  // The cell should have a flash class applied.
  const cell = mount.querySelector(
    '.sim-ribbon-cell[data-sim-sym="USDC"] [data-sim-val]');
  assert.ok(cell, 'ribbon value cell should still exist after tick');
  assert.ok(
    cell.classList.contains('sim-flash-up'),
    'value cell should flash up when value increased');
  assert.match(cell.textContent, /\+2\.50bp/);
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
