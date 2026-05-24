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
