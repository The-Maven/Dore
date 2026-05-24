/**
 * Test fixtures — minimal shapes mirroring what the FastAPI endpoints
 * serialise. Shape stays in sync with src/sca/models.py manually for now;
 * if it drifts, a render test will start failing on missing fields and
 * we'll know to update.
 */

export function supplyFixture(overrides = {}) {
  return {
    symbol: 'USDC',
    total_supply: 50_000_000_000,
    per_chain: [
      {
        chain: 'ethereum',
        contract: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
        raw: 32_000_000_000_000_000, decimals: 6,
        supply: 32_000_000_000,
        kind: 'native', verified: true, verification_method: 'human',
        consensus: '2/2 agree', endpoint: 'https://ethereum-rpc.publicnode.com',
      },
      {
        chain: 'base',
        contract: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        raw: 18_000_000_000_000_000, decimals: 6,
        supply: 18_000_000_000,
        kind: 'native', verified: true, verification_method: 'auto: on-chain symbol match',
        consensus: '1/2 single source', endpoint: 'https://base-rpc.publicnode.com',
      },
    ],
    warnings: [],
    native_supply: 50_000_000_000, bridged_supply: 0,
    read_at: new Date().toISOString(),
    complete: true, chains_expected: 2, chains_read: 2, failed_chains: [],
    ...overrides,
  };
}

export function attestationFixture(overrides = {}) {
  return {
    symbol: 'USDC', as_of_date: '2026-04-30',
    total_reserves: 50_500_000_000,
    tokens_outstanding: 50_400_000_000,
    breakdown: [
      { asset_class: 'cash', amount: 5_000_000_000 },
      { asset_class: 't-bills', amount: 45_500_000_000 },
    ],
    source_url: 'https://www.circle.com/transparency/attestation-2026-04.pdf',
    source_pages: [1, 2, 3], confidence: 0.92,
    ...overrides,
  };
}

export function metricsFixture(overrides = {}) {
  return {
    attested_coverage: 1.002,
    live_coverage: 1.002,
    staleness_days: 10,
    supply_drift: -0.008,
    provenance: '1 issuer attestation · 2/2 chains read · 1 cross-RPC corroborated',
    ...overrides,
  };
}

export function analysisFixture(overrides = {}) {
  return {
    symbol: 'USDC',
    supply: overrides.supply || supplyFixture(),
    attestation: overrides.attestation === undefined ? attestationFixture() : overrides.attestation,
    metrics: overrides.metrics === undefined ? metricsFixture() : overrides.metrics,
    passages: [],
    checks: [
      { name: 'supply_resolved', passed: true, severity: 'info', detail: '50B from 2 chain(s)' },
      { name: 'reserves_positive', passed: true, severity: 'info', detail: 'total_reserves=50.5B' },
    ],
    narrative: '',
    gaps: [],
    augmentations: overrides.augmentations || [],
    backing_model: overrides.backing_model || 'fiat_reserves',
    protocol_url: overrides.protocol_url || '',
  };
}

export function sanctionsFixture(overrides = {}) {
  return {
    symbol: 'USDC',
    supply: supplyFixture(),
    screened: ['0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
               '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'],
    hits: [],
    sdn_publish_date: '05/21/2026', sdn_staleness_days: 2,
    sdn_address_count: 423,
    checks: [
      { name: 'sdn_list_loaded', passed: true, severity: 'critical', detail: '423 addresses loaded' },
      { name: 'sdn_list_fresh', passed: true, severity: 'warn', detail: 'published 05/21/2026 — 2 days ago' },
      { name: 'no_sanctioned_addresses', passed: true, severity: 'critical', detail: 'no screened address is OFAC-sanctioned' },
    ],
    passages: [], narrative: '', gaps: [],
    augmentations: overrides.augmentations || [],
    backing_model: overrides.backing_model || 'fiat_reserves',
    protocol_url: overrides.protocol_url || '',
    ...overrides,
  };
}

export function marketFixture(overrides = {}) {
  // Mirrors the /api/market response just enough that renderMarket runs.
  // Freshness block: covers the Phase 3 footer that names both clocks
  // (extracted window + last validation).
  const now = new Date();
  const hourAgo = new Date(now.getTime() - 3_600_000).toISOString();
  const weekAgo = new Date(now.getTime() - 7 * 86_400_000).toISOString();
  return {
    summary: {
      total_supply: 230_000_000_000, token_count: 20,
      issuer_count: 12, chain_count: 18,
      verified_count: 10, stale_count: 2,
      by_design_count: 5, blocked_count: 3,
    },
    by_issuer: [
      { issuer: 'Circle', count: 1, total_supply: 80e9,
        share_pct: 34.78, tokens: ['USDC'] },
      { issuer: 'Tether', count: 1, total_supply: 110e9,
        share_pct: 47.83, tokens: ['USDT'] },
    ],
    by_backing_model: [
      { model: 'fiat_reserves', label: 'Fiat reserves',
        count: 12, total_supply: 200e9, share_pct: 87,
        tokens: ['USDC', 'USDT'] },
    ],
    by_chain: [
      { chain: 'ethereum', total_supply: 110e9, share_pct: 47.83,
        token_count: 18, tokens: ['USDC', 'USDT'] },
    ],
    concentration: { top3_share_pct: 90.1, top5_share_pct: 97.3, hhi: 0.42 },
    verification_health: {
      fresh: [{ symbol: 'USDC', issuer: 'Circle',
        url: 'https://example.com/usdc.pdf',
        resolved_at: hourAgo }],
      stale: [], by_design: [], blocked: [],
    },
    drift_leaderboard: [],
    recent_signals: [],
    brief: null,
    freshness: {
      earliest_extracted_at: weekAgo,
      latest_extracted_at: hourAgo,
      latest_validated_at: hourAgo,
    },
    computed_at: now.toISOString(),
    elapsed_s: 0.6,
    ...overrides,
  };
}

export function redemptionFixture(overrides = {}) {
  const att = overrides.attestation === undefined ? attestationFixture() : overrides.attestation;
  return {
    symbol: 'USDC',
    supply: supplyFixture(),
    attestation: att,
    metrics: overrides.metrics === undefined ? metricsFixture() : overrides.metrics,
    tiers: overrides.tiers || [
      { tier: 'L1', asset_class: 'cash', amount: 5_000_000_000 },
      { tier: 'L2', asset_class: 't-bills', amount: 45_500_000_000 },
    ],
    liquid_reserves: overrides.liquid_reserves !== undefined ? overrides.liquid_reserves : 50_500_000_000,
    liquid_coverage: overrides.liquid_coverage !== undefined ? overrides.liquid_coverage : 1.01,
    net_redemption_flow: overrides.net_redemption_flow !== undefined ? overrides.net_redemption_flow : -400_000_000,
    checks: [], passages: [], narrative: '', gaps: [],
    augmentations: overrides.augmentations || [],
    backing_model: overrides.backing_model || 'fiat_reserves',
    protocol_url: overrides.protocol_url || '',
  };
}
