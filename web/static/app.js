/* ════════════════════════════════════════════════════════════════════
   Doré — by Rayleigh Stark. Stablecoin reserve-verification terminal SPA.
   Vanilla JS, no build. Hash routing. Talks to the FastAPI server.
   The opening view is a live operations feed driven by REAL polling of
   /api/supply/{symbol} — every log line narrates actual agent work.
   ════════════════════════════════════════════════════════════════════ */

'use strict';

// ── tiny DOM + format helpers ────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const app = $('#app');

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function')
      node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const icon = (id, cls = '') => {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  if (cls) svg.setAttribute('class', cls);
  svg.setAttribute('viewBox', '0 0 24 24');
  const use = document.createElementNS(ns, 'use');
  use.setAttribute('href', '#' + id);
  svg.append(use);
  return svg;
};

// ── instrument brand marks ───────────────────────────────────────────
// Tokens with a recognisable mark get a hand-drawn inline SVG symbol
// (sprite in index.html). The rest get a tasteful bordered monogram.
const TOKEN_SYMBOLS = new Set(['USDC', 'EURC', 'USDT', 'PYUSD', 'USDP', 'GUSD']);
// monogram fallbacks — initials shown in a bordered box mark
const TOKEN_MONOGRAM = { USDG: 'UG', TUSD: 'TU', FDUSD: 'FD' };

function tokenMark(symbol, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const sym = String(symbol || '').toUpperCase();
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', ('tmark ' + cls).trim());
  // brand colour carried as a data attribute — CSS lights it up only
  // when the instrument is the selected/active one.
  if (sym) svg.setAttribute('data-token', sym);
  if (TOKEN_SYMBOLS.has(sym)) {
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#t-' + sym);
    svg.append(use);
    return svg;
  }
  // monogram fallback — bordered box + initials
  const text = TOKEN_MONOGRAM[sym] || sym.slice(0, 2);
  const box = document.createElementNS(ns, 'rect');
  box.setAttribute('x', '2.5'); box.setAttribute('y', '2.5');
  box.setAttribute('width', '19'); box.setAttribute('height', '19');
  box.setAttribute('rx', '2.5');
  box.setAttribute('fill', 'none'); box.setAttribute('stroke', 'currentColor');
  box.setAttribute('stroke-width', '1.6');
  const t = document.createElementNS(ns, 'text');
  t.setAttribute('x', '12'); t.setAttribute('y', '13');
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('dominant-baseline', 'central');
  t.setAttribute('fill', 'currentColor');
  t.setAttribute('font-size', text.length > 2 ? '7' : '9');
  t.setAttribute('font-weight', '700');
  t.setAttribute('font-family', "'IBM Plex Mono', monospace");
  t.setAttribute('letter-spacing', '-.5');
  t.textContent = text;
  svg.append(box, t);
  return svg;
}

// ── chain marks ──────────────────────────────────────────────────────
// Each known chain gets a hand-drawn inline SVG symbol (sprite in
// index.html). Unknown chains fall back to a bordered monogram, mirroring
// the tokenMark() treatment.
const CHAIN_SYMBOLS = new Set(
  ['ethereum', 'arbitrum', 'base', 'optimism', 'polygon', 'solana']);

function chainMark(chain, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const key = String(chain || '').toLowerCase().trim();
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', ('cmark ' + cls).trim());
  if (key) svg.setAttribute('data-chain', key);
  if (CHAIN_SYMBOLS.has(key)) {
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#c-' + key);
    svg.append(use);
    return svg;
  }
  // monogram fallback — bordered box + first two letters
  const text = key.slice(0, 2).toUpperCase() || '??';
  const box = document.createElementNS(ns, 'rect');
  box.setAttribute('x', '2.5'); box.setAttribute('y', '2.5');
  box.setAttribute('width', '19'); box.setAttribute('height', '19');
  box.setAttribute('rx', '2.5');
  box.setAttribute('fill', 'none'); box.setAttribute('stroke', 'currentColor');
  box.setAttribute('stroke-width', '1.6');
  const t = document.createElementNS(ns, 'text');
  t.setAttribute('x', '12'); t.setAttribute('y', '13');
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('dominant-baseline', 'central');
  t.setAttribute('fill', 'currentColor');
  t.setAttribute('font-size', '9');
  t.setAttribute('font-weight', '700');
  t.setAttribute('font-family', "'IBM Plex Mono', monospace");
  t.setAttribute('letter-spacing', '-.5');
  t.textContent = text;
  svg.append(box, t);
  return svg;
}

// block-explorer base URLs — append the token contract address
const CHAIN_EXPLORERS = {
  ethereum:  'https://etherscan.io/token/',
  arbitrum:  'https://arbiscan.io/token/',
  base:      'https://basescan.org/token/',
  optimism:  'https://optimistic.etherscan.io/token/',
  polygon:   'https://polygonscan.com/token/',
  bsc:       'https://bscscan.com/token/',
  avalanche: 'https://snowtrace.io/token/',
  solana:    'https://solscan.io/token/',
  // Tron uses the #/contract/{address} hash route on TronScan — the
  // contract page shows symbol, decimals, holders, and recent transfers,
  // matching the credibility of the etherscan token pages we link for
  // the EVM chains.
  tron:      'https://tronscan.org/#/contract/',
};
function explorerUrl(chain, contract) {
  const base = CHAIN_EXPLORERS[String(chain || '').toLowerCase().trim()];
  return base && contract ? base + contract : null;
}

// Render a small badge for the per-chain RPC consensus.
// "2/2 agree" = best, "1/2 single source" = degraded, "DISAGREEMENT" = bad.
function consensusBadge(label) {
  if (!label) return el('span', { class: 'consensus consensus-mute' }, '—');
  const s = String(label);
  let cls = 'consensus-mute';
  let tip = label;
  // DISAGREEMENT must be checked first — the word "AGREE" is a substring,
  // so a naive /agree/ regex would misclassify it as the green path.
  if (/DISAGREEMENT/i.test(s)) {
    cls = 'consensus-bad';
    tip = 'RPC endpoints returned different values. '
        + 'Do not trust this read — investigate immediately.';
  } else if (/\bagree\b/i.test(s)) {
    cls = 'consensus-ok';
    tip = 'Two RPC endpoints returned the same value — strong corroboration.';
  } else if (/single source/i.test(s)) {
    cls = 'consensus-warn';
    tip = 'Only one RPC endpoint responded — value is uncorroborated. '
        + 'Fallback unreachable; check endpoint health.';
  } else if (/majority/i.test(s)) {
    cls = 'consensus-ok';
    tip = 'Two of three RPCs agreed; outlier ignored.';
  } else if (/primary|fallback/i.test(s)) {
    cls = 'consensus-mute';
    tip = 'Sequential read — primary endpoint sufficient; no corroboration attempted.';
  }
  return el('span', { class: 'consensus ' + cls, title: tip }, s);
}

// Render a small dot+label badge for a corpus source's snapshot health.
// Drives both the per-passage card and the global "needs attention" banner.
function sourceHealthBadge(src) {
  const status = (src && src.snapshotStatus) || 'unknown';
  let cls = 'src-health-mute';
  let label = status;
  let tip = '';
  if (status === 'live') {
    cls = 'src-health-ok';
    label = 'live';
    tip = 'Last fetched cleanly. Archived copy on file as a fallback.';
  } else if (status === 'broken') {
    cls = 'src-health-bad';
    label = 'live broken';
    tip = 'The live URL is down or 4xx/5xx. Showing the archived copy. '
        + 'This source needs a maintainer to re-anchor.';
  } else {
    cls = 'src-health-mute';
    label = 'not canaried';
    tip = 'No snapshot on record yet — run `sca canary` to fetch one.';
  }
  if (src && src.snapshotAgeDays != null) {
    tip += ` (archived ${src.snapshotAgeDays} day(s) ago)`;
  }
  return el('span', { class: 'src-health ' + cls, title: tip }, label);
}

// Backing-model strip — names what's actually backing this token, so a
// missing fiat attestation reads "by design" (crypto-collateralized) vs.
// a real gap (fiat-backed, fetch failed). Always visible above the
// metric grid so the reader sees the lineage before the numbers.
const BACKING_LABELS = {
  fiat_reserves: {
    label: 'fiat reserves',
    cls: 'backing-fiat',
    desc: 'Backed by off-chain cash, treasuries, or equivalents — an issuer publishes periodic attestations by an independent CPA.',
  },
  crypto_collateral: {
    label: 'crypto collateral',
    cls: 'backing-crypto',
    desc: 'Backed by on-chain collateral managed by a smart-contract protocol — backing is visible on-chain, not via a PDF.',
  },
  synthetic_delta_neutral: {
    label: 'synthetic · delta-neutral',
    cls: 'backing-synthetic',
    desc: 'Backed by delta-neutral positions (e.g. staked ETH + short perpetuals). Live reserves on the issuer dashboard.',
  },
  algorithmic: {
    label: 'algorithmic',
    cls: 'backing-algorithmic',
    desc: 'Stabilised by an algorithmic mechanism plus partial collateral. Composition varies — see protocol dashboard.',
  },
  new_or_unverified: {
    label: 'new · unverified',
    cls: 'backing-new',
    desc: 'Recently launched. No mature published attestation system yet — treat any backing claim with caution.',
  },
};

// Data-lineage banner — the audit-grade artefact. Sits above the snapshot
// and names exactly how every figure below was sourced: how many chains
// read, how many endpoints agreed, whether the read is partial. This is
// the visible bridge between "we run multi-RPC" (a feature) and "you can
// see we ran multi-RPC" (a trust signal). World-class compliance UI
// should never make a user dig for provenance.
function dataLineageBanner(supply, metrics) {
  if (!supply) return el('span');
  const chainsRead = supply.chains_read != null ? supply.chains_read
                   : (supply.per_chain || []).length;
  const chainsExp = supply.chains_expected != null ? supply.chains_expected
                  : chainsRead;
  const complete = supply.complete !== false;
  const corroborated = (supply.per_chain || [])
    .filter((c) => /\bagree\b/i.test(c.consensus || '')).length;
  const single = (supply.per_chain || [])
    .filter((c) => /single source/i.test(c.consensus || '')).length;
  const disagree = (supply.per_chain || [])
    .filter((c) => /DISAGREEMENT/i.test(c.consensus || '')).length;

  const cls = !complete ? 'lineage-partial'
            : disagree > 0 ? 'lineage-disagree'
            : corroborated >= chainsRead ? 'lineage-strong'
            : corroborated > 0 ? 'lineage-mixed' : 'lineage-thin';
  const headline = !complete
    ? `PARTIAL — ${chainsRead}/${chainsExp} chains read`
    : disagree > 0
      ? `DATA LINEAGE — ${chainsRead} chains, ${disagree} disagreement(s) needs review`
      : `DATA LINEAGE — ${chainsRead} chain${chainsRead === 1 ? '' : 's'} read`;
  const breakdown = [];
  if (corroborated > 0) breakdown.push(`${corroborated} cross-RPC corroborated`);
  if (single > 0) breakdown.push(`${single} single-source`);
  if (chainsExp > chainsRead) {
    const failed = (supply.failed_chains || []).join(', ');
    breakdown.push(`failed: ${failed || (chainsExp - chainsRead) + ' chain(s)'}`);
  }
  const sub = breakdown.length ? breakdown.join(' · ')
    : 'single-endpoint reads (no fallback pool configured for this chain)';
  return el('div', { class: 'lineage-banner ' + cls,
    title: 'Multi-chain reads with cross-RPC corroboration. Doré reads '
      + 'each chain from multiple RPC endpoints in parallel and requires '
      + 'agreement on supply figures — a single misbehaving RPC cannot '
      + 'poison the result.' },
    el('span', { class: 'lineage-kick' }, headline),
    el('span', { class: 'lineage-sub' }, sub),
    metrics && metrics.provenance
      ? el('span', { class: 'lineage-prov' },
          el('span', { class: 'glyph' }, '§'),
          'metrics: ' + metrics.provenance)
      : null,
  );
}

// ── Doré Brief hero panel ─────────────────────────────────────────────
// Editorial top-of-view synthesis. Distinct visual identity (gold border,
// large headline, kicker badge) so a reader recognises it as an AI brief
// in the first glance — never confused with a verified figure.
function aiBriefHero(brief) {
  if (!brief || !brief.headline) return el('span');
  const surfaceLabel = {
    'analyze': 'ATTESTATION',
    'sanctions': 'SANCTIONS',
    'redemption': 'REDEMPTION',
  }[brief.surface] || (brief.surface || '').toUpperCase();
  const generated = brief.generated_at
    ? new Date(brief.generated_at).toLocaleString()
    : '';

  const hero = el('div', { class: 'brief-hero fade-in' });
  // Kicker row: DORÉ BRIEF badge + surface + symbol + generated time
  hero.append(el('div', { class: 'brief-kick-row' },
    el('span', { class: 'brief-badge' }, 'DORÉ BRIEF'),
    el('span', { class: 'brief-surface' }, surfaceLabel),
    el('span', { class: 'brief-symbol' }, brief.symbol || ''),
    generated
      ? el('span', { class: 'brief-time',
          title: 'AI brief composed at ' + generated },
          'composed ' + generated)
      : el('span'),
  ));
  // Headline — the bottom line, large editorial type
  hero.append(el('div', { class: 'brief-headline' }, brief.headline));
  // Key points — 2-4 bullets, scannable
  if ((brief.key_points || []).length) {
    const kp = el('ul', { class: 'brief-points' });
    brief.key_points.forEach((p) =>
      kp.append(el('li', { class: 'brief-point' }, p)));
    hero.append(kp);
  }
  // Relevant news — auto-discovered corpus items the LLM judged relevant
  if ((brief.relevant_news || []).length) {
    const news = el('div', { class: 'brief-news' },
      el('div', { class: 'brief-news-kick' }, 'RELEVANT IN THE CORPUS'));
    brief.relevant_news.forEach((item) => {
      const row = el('div', { class: 'brief-news-row' });
      if (item.url) {
        row.append(el('a', {
          class: 'brief-news-title',
          href: item.url, target: '_blank', rel: 'noopener noreferrer',
          title: 'open ' + item.url + ' in a new tab',
        }, item.title || item.source || 'untitled', ' ',
          el('span', { class: 'glyph' }, '↗')));
      } else {
        row.append(el('span', { class: 'brief-news-title' },
          item.title || item.source || 'untitled'));
      }
      if (item.source) {
        row.append(el('span', { class: 'brief-news-src' }, item.source));
      }
      news.append(row);
    });
    hero.append(news);
  }
  // Provenance footer: short, present-tense, ownership-first. Tells the
  // reader the brief's lineage without sounding defensive.
  hero.append(el('div', { class: 'brief-foot' },
    el('span', {}, 'Composed by an LLM from the verified figures above '
      + 'and the cited corpus below. Numbers are lifted verbatim from the '
      + 'pipeline; the prose connects them.')));
  return hero;
}

// ── backing-model-aware n/a copy ─────────────────────────────────────
// The coverage cells used to dead-end with "No current attestation could
// be resolved — see GAPS" regardless of the token. That line is wrong
// for crypto-collateralised, synthetic, and algorithmic tokens — they
// don't HAVE a fiat attestation by design. These helpers read the
// backing model and frame the missing-coverage state correctly per
// category, with a working link to where live data actually lives.
function _backingNaCopy(a) {
  const model = a && a.backing_model || 'fiat_reserves';
  const proto = a && a.protocol_url || '';
  const issuer = a && a.symbol ? a.symbol : 'this token';
  if (model === 'crypto_collateral') {
    return {
      kicker: 'BACKING — ON-CHAIN COLLATERAL',
      big: '∞',  // mathematical "outside this metric's domain"
      desc: 'A fiat coverage ratio doesn\'t apply: ' + issuer + ' is '
        + 'over-collateralised by on-chain assets held in protocol vaults. '
        + 'Live backing composition is visible on-chain (not via a PDF), '
        + 'and the AI Context below points to the protocol dashboard.',
      protoLabel: 'view live collateral',
    };
  }
  if (model === 'synthetic_delta_neutral') {
    return {
      kicker: 'BACKING — DELTA-NEUTRAL POSITIONS',
      big: '◇',
      desc: issuer + ' is backed by dynamic hedged positions (e.g. staked '
        + 'ETH offset by short perpetuals); a single static coverage '
        + 'ratio doesn\'t describe it. Reserves are visible on the issuer '
        + 'live dashboard rather than a periodic attestation.',
      protoLabel: 'view live reserves dashboard',
    };
  }
  if (model === 'algorithmic') {
    return {
      kicker: 'BACKING — HYBRID ALGORITHMIC',
      big: '⌬',
      desc: issuer + ' uses an algorithmic stabilisation mechanism plus '
        + 'partial on-chain collateral. Composition varies and isn\'t '
        + 'reducible to a fiat coverage ratio — treat any attestation '
        + 'claim with caution.',
      protoLabel: 'view protocol dashboard',
    };
  }
  if (model === 'new_or_unverified') {
    return {
      kicker: 'BACKING — NEW · UNVERIFIED',
      big: '—',
      desc: issuer + ' is recently launched and has no mature published '
        + 'attestation system yet. Any backing claim should be treated '
        + 'with caution until an independent attestation appears.',
      protoLabel: 'issuer page',
    };
  }
  // Default: fiat_reserves where the fetch failed. Quiet copy: do NOT
  // lead with "fetch unavailable" or "no attestation available" — those
  // read as broken product. The AI Context card above already carries
  // the qualitative answer; this row stays neutral and points to it.
  return {
    kicker: 'ATTESTED COVERAGE',
    big: '—',
    desc: 'Reserves and coverage ratios populate from a current CPA '
      + 'attestation PDF. The AI Context above describes ' + issuer
      + '’s backing structure and where the live report lives; '
      + 'on-chain supply figures are unaffected.',
    protoLabel: 'issuer transparency page',
  };
}

function naCoverageDesc(a) {
  const c = _backingNaCopy(a);
  const proto = a && a.protocol_url || '';
  const children = [c.desc];
  if (proto && c.protoLabel) {
    children.push(' ');
    children.push(el('a', {
      href: proto, target: '_blank', rel: 'noopener noreferrer',
      class: 'na-proto-link',
      title: 'open ' + proto + ' in a new tab',
    }, c.protoLabel, ' ', el('span', { class: 'glyph' }, '↗')));
  }
  return naFieldNote(...children);
}

function coverageKickerForNa(a, defaultLabel) {
  const c = _backingNaCopy(a);
  return c.kicker || defaultLabel;
}

function naCoverageBig(a) {
  return _backingNaCopy(a).big;
}

function backingModelStrip(a) {
  const model = a && a.backing_model || 'fiat_reserves';
  const info = BACKING_LABELS[model] || BACKING_LABELS.fiat_reserves;
  const protoUrl = a && a.protocol_url || '';
  const children = [
    el('span', { class: 'backing-kicker' }, 'BACKING MODEL'),
    el('span', { class: 'backing-badge ' + info.cls, title: info.desc },
      info.label),
    el('span', { class: 'backing-desc' }, info.desc),
  ];
  if (protoUrl) {
    children.push(el('a', {
      class: 'backing-link', href: protoUrl,
      target: '_blank', rel: 'noopener noreferrer',
      title: 'open the protocol / dashboard where live backing data lives',
    }, 'protocol ', el('span', { class: 'glyph' }, '↗')));
  }
  return el('div', { class: 'backing-strip' }, ...children);
}

// Augmentation card — LLM-generated context, clearly tagged "AI CONTEXT".
// Surfaces qualitative info (backing model, attestation cadence, where to
// find live data) when a deterministic source couldn't be resolved.
// Never carries numeric figures.
function augmentationCard(ctx) {
  if (!ctx || !ctx.text) return el('span');
  const confidence = ctx.confidence || 'training-data-only';
  const conf = confidence === 'web-searched'
    ? { label: 'web search + model',
        cls: 'aug-conf-web',
        title: 'Composed by an LLM with grounding from a live web search '
          + 'on the issuer’s name and recent regulatory news. Numbers '
          + 'come from the verified pipeline only, never from the LLM.' }
    : { label: 'model knowledge',
        cls: 'aug-conf-training',
        title: 'Composed by an LLM from its training data; the pipeline '
          + 'never injects numeric figures via the LLM.' };
  const reasonLabel = {
    'no-fiat-attestation-by-design':
      'this backing model has no fiat attestation',
    'live-attestation-fetch-failed':
      'fresh attestation PDF not yet resolved',
  }[ctx.reason] || ctx.reason || '';
  const head = el('div', { class: 'aug-head' },
    el('span', { class: 'aug-tag' }, 'AI CONTEXT'),
    el('span', { class: 'aug-conf ' + conf.cls, title: conf.title },
      conf.label));
  if (reasonLabel) {
    head.append(el('span', { class: 'aug-reason' }, '· ' + reasonLabel));
  }
  const card = el('div', { class: 'aug-card' }, head,
    el('div', { class: 'aug-body' }, ctx.text));
  if ((ctx.citations || []).length) {
    const cites = el('div', { class: 'aug-cites' },
      el('span', { class: 'aug-cites-kick' }, 'cited:'));
    ctx.citations.forEach((url, i) => {
      if (i > 0) cites.append(el('span', { class: 'aug-cite-sep' }, ' · '));
      cites.append(el('a', {
        class: 'aug-cite', href: url,
        target: '_blank', rel: 'noopener noreferrer',
        title: 'open ' + url + ' in a new tab',
      }, url.replace(/^https?:\/\//, '').replace(/\/.*$/, ''),
         el('span', { class: 'glyph' }, ' ↗')));
    });
    card.append(cites);
  }
  return card;
}

const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function fmtNum(n, dp = 0) {
  if (n == null || Number.isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US',
    { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function fmtUSD(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + fmtNum(n, 2);
}
// compact magnitude — no $ prefix, for dense log columns (e.g. 73.15B)
function fmtMag(n) {
  if (n == null || Number.isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(Math.round(n));
}
// signed percentage delta — '+0.4%' / '-0.2%' / 'flat'
function fmtDelta(r) {
  if (r == null || Number.isNaN(r) || !isFinite(r)) return 'flat';
  const p = r * 100;
  if (Math.abs(p) < 0.005) return 'flat';
  return (p > 0 ? '+' : '') + p.toFixed(2) + '%';
}
function deltaClass(r) {
  if (r == null || Math.abs(r) < 0.00005) return '';
  return r > 0 ? 'd-up' : 'd-dn';
}
function fmtPct(r) {
  if (r == null || Number.isNaN(r)) return 'n/a';
  return (r * 100).toFixed(2) + '%';
}
function covClass(r) {
  if (r == null) return 'v-muted';
  if (r >= 1.0) return 'v-green';
  if (r >= 0.98) return 'v-amber';
  return 'v-rose';
}
const pad2 = (n) => String(n).padStart(2, '0');
function clockStr(d = new Date()) {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

// a visible cue when a run is attempted with an empty SYMBOL field — a
// brief shake + a placeholder prompt, so the button never feels dead.
function flagEmptyInput(input) {
  if (!input) return;
  input.focus();
  input.classList.remove('input-nudge');
  // force reflow so the animation re-triggers on a repeated empty click
  void input.offsetWidth;
  input.classList.add('input-nudge');
  const orig = input.getAttribute('placeholder') || 'SYMBOL';
  input.setAttribute('placeholder', 'ENTER A SYMBOL');
  setTimeout(() => {
    input.classList.remove('input-nudge');
    input.setAttribute('placeholder', orig);
  }, 1400);
}

async function api(path, opts) {
  opts = opts || {};
  // attach the Supabase access token when signed in — anonymous calls
  // simply omit it and the server treats them as anonymous (never rejected).
  const token = AUTH.token();
  if (token) {
    opts = Object.assign({}, opts, {
      headers: Object.assign({}, opts.headers,
        { Authorization: 'Bearer ' + token }),
    });
  }
  const res = await fetch('/api' + path, opts);
  let body;
  try { body = await res.json(); } catch { body = {}; }
  if (!res.ok) {
    const err = new Error(body.detail || body.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ════════════════════════════════════════════════════════════════════
//  OPTIONAL AUTHENTICATION
//  Auth is optional and gates *persistence* (saved history, curation
//  votes, a profile) — never functional use. The whole terminal works
//  anonymously; an account just lets work be saved. supabase-js owns
//  session persistence + token refresh; the server validates the JWT.
// ════════════════════════════════════════════════════════════════════
const AUTH = {
  client: null,        // supabase-js client, or null when unconfigured
  session: null,       // current Supabase session, or null (anonymous)
  user: null,          // { id, email } or null
  profile: null,       // profiles row, or null
  _mode: 'in',         // sign-in | sign-up panel mode

  token() { return this.session ? this.session.access_token : null; },
  signedIn() { return !!this.user; },

  // ── bootstrap: pull public config, init supabase-js, restore session ──
  async init() {
    let cfg;
    try { cfg = await api('/config'); } catch { cfg = {}; }
    if (!cfg.supabase_url || !cfg.supabase_anon_key
        || typeof window.supabase === 'undefined') {
      // Supabase not configured / CDN unavailable — stay anonymous-only.
      this._renderControl();
      return;
    }
    this.client = window.supabase.createClient(
      cfg.supabase_url, cfg.supabase_anon_key);
    const { data } = await this.client.auth.getSession();
    this.session = data ? data.session : null;
    // react to refresh / sign-in / sign-out across tabs
    this.client.auth.onAuthStateChange((_evt, session) => {
      this.session = session;
      if (!session) { this.user = null; this.profile = null; }
      this._renderControl();
    });
    if (this.session) { await this._loadMe(); }
    this._renderControl();
    this._wirePanel();
  },

  async _loadMe() {
    try {
      const me = await api('/me');
      this.user = me.user;
      this.profile = me.profile;
    } catch { this.user = null; this.profile = null; }
  },

  // ── topbar control: SIGN IN  ⇄  the signed-in identity + SIGN OUT ──
  _renderControl() {
    const ctl = $('#auth-ctl');
    const dot = $('#auth-dot');
    const label = $('#auth-label');
    if (!ctl) return;
    if (!this.client) {
      // no auth backend — keep the control quiet and inert
      ctl.style.display = 'none';
      return;
    }
    ctl.style.display = '';
    if (this.signedIn()) {
      dot.classList.add('in');
      const who = (this.user.email || 'account').split('@')[0];
      label.textContent = who.toUpperCase() + ' · SIGN OUT';
      ctl.title = 'Signed in as ' + this.user.email + ' — click to sign out';
      ctl.onclick = () => this.signOut();
    } else {
      dot.classList.remove('in');
      label.textContent = 'SIGN IN';
      ctl.title = 'Sign in to save your work — the terminal works without it';
      ctl.onclick = () => this.openPanel('in');
    }
  },

  // ── the sign-in / sign-up panel ──────────────────────────────────
  openPanel(mode, note) {
    if (!this.client) return;  // nothing to sign into
    this._mode = mode || 'in';
    const ov = $('#auth-overlay');
    ov.hidden = false;
    this._syncPanelMode();
    const msg = $('#auth-msg');
    if (note) { msg.hidden = false; msg.className = 'auth-msg'; msg.textContent = note; }
    else { msg.hidden = true; }
    setTimeout(() => { const e = $('#auth-email'); if (e) e.focus(); }, 30);
  },
  closePanel() { $('#auth-overlay').hidden = true; },

  _syncPanelMode() {
    const up = this._mode === 'up';
    $('#auth-tab-in').classList.toggle('active', !up);
    $('#auth-tab-up').classList.toggle('active', up);
    $('#auth-title').textContent = up ? 'CREATE ACCOUNT' : 'SIGN IN';
    $('#auth-submit').textContent = up ? 'CREATE ACCOUNT' : 'SIGN IN';
    $('#auth-pass').setAttribute('autocomplete',
      up ? 'new-password' : 'current-password');
    $('#auth-foot').textContent = up
      ? 'Already have an account? Use the SIGN IN tab.'
      : 'No account yet? CREATE ACCOUNT — it is free and instant.';
  },

  _wirePanel() {
    $('#auth-close').onclick = () => this.closePanel();
    $('#auth-overlay').addEventListener('click', (e) => {
      if (e.target.id === 'auth-overlay') this.closePanel();
    });
    $('#auth-tab-in').onclick = () => { this._mode = 'in'; this._syncPanelMode(); };
    $('#auth-tab-up').onclick = () => { this._mode = 'up'; this._syncPanelMode(); };
    $('#auth-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this._submit();
    });
    const g = $('#oauth-google');
    const a = $('#oauth-apple');
    if (g) g.onclick = () => this._oauth('google', 'Google');
    if (a) a.onclick = () => this._oauth('apple', 'Apple');
  },

  // ── OAuth sign-in — Google / Apple via supabase-js ─────────────────
  // supabase-js owns the provider redirect; on return the existing
  // getSession() / onAuthStateChange handlers pick the session up. If a
  // provider is not yet enabled in Supabase the call returns an error —
  // surfaced as a calm inline message, never a crash.
  async _oauth(provider, label) {
    const msg = $('#auth-msg');
    const show = (cls, text) => {
      msg.hidden = false; msg.className = 'auth-msg ' + cls; msg.textContent = text;
    };
    const gBtn = $('#oauth-google');
    const aBtn = $('#oauth-apple');
    if (gBtn) gBtn.disabled = true;
    if (aBtn) aBtn.disabled = true;
    try {
      const { error } = await this.client.auth.signInWithOAuth({
        provider,
        options: { redirectTo: window.location.origin },
      });
      if (error) throw new Error(error.message);
      // success — supabase-js is navigating away to the provider; nothing
      // more to do here, the redirect takes over.
    } catch (err) {
      const raw = String((err && err.message) || err || '');
      const notEnabled = /provider is not enabled|not enabled|unsupported provider/i
        .test(raw);
      show('err', notEnabled
        ? label + ' sign-in isn’t configured yet — use email and password below.'
        : label + ' sign-in could not start: ' + raw);
      if (gBtn) gBtn.disabled = false;
      if (aBtn) aBtn.disabled = false;
    }
  },

  async _submit() {
    const email = $('#auth-email').value.trim();
    const pass = $('#auth-pass').value;
    const msg = $('#auth-msg');
    const btn = $('#auth-submit');
    const show = (cls, text) => {
      msg.hidden = false; msg.className = 'auth-msg ' + cls; msg.textContent = text;
    };
    if (!email || pass.length < 6) {
      show('err', 'Enter an email and a password of at least 6 characters.');
      return;
    }
    btn.disabled = true;
    const wasUp = this._mode === 'up';
    try {
      const fn = wasUp ? 'signUp' : 'signInWithPassword';
      const { data, error } = await this.client.auth[fn]({ email, password: pass });
      if (error) throw new Error(error.message);
      if (wasUp && (!data.session)) {
        // email-confirmation flow — no session returned
        show('ok', 'Account created. Check your inbox to confirm, then sign in.');
        this._mode = 'in'; this._syncPanelMode();
        btn.disabled = false;
        return;
      }
      this.session = data.session;
      await this._loadMe();
      this._renderControl();
      this.closePanel();
      logLine('OK', 'AUTH', [
        seg(wasUp ? 'account created' : 'signed in', 'lg-val'),
        seg(this.user ? this.user.email : email),
        seg('persistence unlocked', 'd-up'),
      ]);
      route();  // re-render so history / signed-in affordances appear
    } catch (err) {
      show('err', String(err.message || err));
    } finally {
      btn.disabled = false;
    }
  },

  async signOut() {
    try { await this.client.auth.signOut(); } catch { /* ignore */ }
    this.session = null; this.user = null; this.profile = null;
    this._renderControl();
    logLine('WORK', 'AUTH', [seg('signed out', 'lg-val'),
      seg('back to anonymous · the terminal stays fully usable')]);
    route();
  },
};

// "Sign in to save" — a small inviting prompt shown when an anonymous
// caller reaches a save-type action. An invitation, never an error.
function savePrompt(what) {
  const btn = el('button', { class: 'btn' }, 'SIGN IN');
  btn.addEventListener('click', () => AUTH.openPanel('in',
    'Sign in to ' + what + '. The terminal stays fully usable either way.'));
  return el('div', { class: 'save-prompt' },
    el('span', { class: 'sp-ic' }, icon('i-human')),
    el('div', { class: 'sp-txt' },
      el('b', {}, 'Sign in to save. '),
      'You can ' + what + ' once you have an account — anonymous runs ',
      'work fully but are not kept.'),
    btn);
}

// ════════════════════════════════════════════════════════════════════
//  SHARED STATE
// ════════════════════════════════════════════════════════════════════
const STATE = {
  tokens: [],            // registry
  supply: {},            // symbol -> last SupplyResult ('error' on failure)
  feed: [],              // {time, level, agent, surface, msg}
  feedSeq: 0,
  activeSymbol: '',      // selected instrument
  monitorRunning: false,
  sources: {},           // source_id → { title, url, tier, verified, included }
  sourcesLoaded: false,  // becomes true on first successful /api/sources fetch
  sourcesLoading: null,  // in-flight promise (so concurrent callers share a load)
};
const FEED_MAX = 320;

// ── corpus source registry — lazy single-shot loader ─────────────────
// The REASONING FRAME passage card looks up its source's display title and
// canonical URL from /api/sources. We load once, cache in STATE.sources,
// and let renderPassage() trigger a background refresh on the first miss.
// The renderer is synchronous (no network in the render path) — a passage
// that arrives before the registry resolves falls back to a humanised
// version of source_id, then re-rendering picks up the loaded title.
async function loadSources() {
  if (STATE.sourcesLoading) return STATE.sourcesLoading;
  STATE.sourcesLoading = (async () => {
    try {
      const data = await api('/sources');
      const map = {};
      (data.sources || []).forEach((s) => {
        if (!s || !s.id) return;
        map[s.id] = {
          title: s.title || '',
          url: s.url || '',
          tier: s.tier || '',
          verified: !!s.verified,
          included: s.included !== false,
          // snapshot health — drives the "live / archived / broken" badge
          // and the auto-fallback from view-source to view-archived-copy.
          snapshotStatus: s.snapshot_status || 'unknown',
          snapshotUrl: s.snapshot_url || '',
          snapshotAgeDays: s.snapshot_age_days,
        };
      });
      STATE.sources = map;
      STATE.sourcesLoaded = true;
    } catch (_e) { /* leave whatever's cached; renderer has a fallback */ }
    finally { STATE.sourcesLoading = null; }
    return STATE.sources;
  })();
  return STATE.sourcesLoading;
}

// Look up a source's display title — falls back to the corpusSourceName()
// humanisation of the registry id when the registry hasn't loaded yet, or
// when an unknown source_id is encountered. Never returns an engineer slug.
function sourceTitle(sourceId) {
  const id = String(sourceId || '').trim();
  if (!id) return 'Unknown source';
  const s = STATE.sources[id];
  if (s && s.title) return s.title;
  // Trigger a background load — the passage renderer is sync; a later view
  // re-render will pick up the resolved title without disrupting this paint.
  if (!STATE.sourcesLoaded) loadSources();
  return corpusSourceName(id);
}

// Look up a source's canonical URL from the registry. Returns '' (not null)
// so callers can compare with a truthy check; an unknown / unresolved
// source simply renders without a "view source" link.
function sourceUrl(sourceId) {
  const id = String(sourceId || '').trim();
  if (!id) return '';
  const s = STATE.sources[id];
  return s && s.url ? s.url : '';
}

// ── hover-tooltip copy — concise, plain-language, terminal-styled ────
// One source of truth for the non-obvious terms surfaced across views.
const tip = {
  attested:
    'Attested coverage — reserves divided by the tokens outstanding stated '
    + 'in the attestation. The issuer’s backing ratio AS OF the attestation '
    + 'date; fixed, unaffected by later supply moves.',
  live:
    'Live coverage — attested reserves divided by CURRENT on-chain supply. '
    + 'Drifts away from the attested ratio as supply mints/burns after the '
    + 'attestation date.',
  bridged:
    'Bridged share — bridged supply ÷ (native + bridged). Wrapped/bridged '
    + 'copies are collateralised by locked native supply; counted separately '
    + 'so the headline isn’t double-counted.',
  staleness:
    'Staleness — days elapsed since the attestation date. Older attestations '
    + 'are weaker evidence of present-day backing.',
  drift:
    'Supply drift — real measured change in on-chain supply between the last '
    + 'two polled reads. Large drift widens attested-vs-live coverage.',
  verified:
    'Verified — the deployment’s contract source is published & matches '
    + 'on-chain bytecode, so the read is trustworthy.',
  unverified:
    'Unverified — contract source not published / not matched. Supply is read '
    + 'but treated with lower confidence.',
  verifiedRatio:
    'Verified deployments ÷ total deployments. All-verified is green; any '
    + 'unverified contract drops it to amber.',
  kindNative:
    'Native — genuine first-party issuance on this chain. Sums into the '
    + 'headline supply figure.',
  kindBridged:
    'Bridged — a wrapped copy minted by a bridge against locked native '
    + 'supply. Shown, but excluded from the headline to avoid double-counting.',
  confidence:
    'Extraction confidence — how reliably the LLM parsed reserve figures out '
    + 'of the attestation PDF. Low scores flag manual review.',
  status: {
    ok: 'LIVE — on-chain supply read succeeded with no warnings.',
    watch: 'WATCH — read succeeded but the agent raised one or more warnings.',
    alert: 'ALERT — the on-chain supply read failed for this instrument.',
    idle: 'PENDING — no read taken yet this session.',
  },
  // guardrail-check names — keyed by a normalised form of the check name.
  // Covers both the real check ids and human-readable variants.
  checks: {
    'supply_resolved':
      'Did the on-chain supply read resolve cleanly across every '
      + 'deployment? Fails if any chain read errored.',
    'reserves_positive':
      'Are the attested reserves a positive figure? Fails if the '
      + 'extracted reserve total is zero or missing.',
    'tokens_positive':
      'Are tokens outstanding a positive figure? Fails if the attested '
      + 'circulating supply is zero or missing.',
    'breakdown_sums_to_total':
      'Do the per-chain supply figures add up to the reported total? '
      + 'Fails on an arithmetic mismatch.',
    'reserve coverage':
      'Are attested reserves ≥ tokens outstanding? Fails if the issuer '
      + 'reports less backing than circulating supply.',
    'attestation freshness':
      'Is the attestation recent enough? Fails when it is too stale to '
      + 'evidence present-day backing.',
    'staleness':
      'Is the attestation recent enough? Fails when it is too stale to '
      + 'evidence present-day backing.',
    'supply drift':
      'Has on-chain supply moved materially since the attestation? Large '
      + 'drift makes the attested ratio unreliable.',
    'drift':
      'Has on-chain supply moved materially since the attestation? Large '
      + 'drift makes the attested ratio unreliable.',
    'extraction confidence':
      'Did the LLM extract reserve figures from the PDF with enough '
      + 'confidence? Low confidence demands manual review.',
    'confidence':
      'Did the LLM extract reserve figures from the PDF with enough '
      + 'confidence? Low confidence demands manual review.',
    'coverage':
      'Do attested reserves still cover on-chain supply? Fails if '
      + 'post-attestation minting outran reserves.',
    'verified':
      'Are all on-chain deployments source-verified? Fails when supply is '
      + 'read from an unverified contract.',
    'bridged':
      'Is the bridged share within tolerance? Flags heavy reliance on '
      + 'bridged copies relative to native issuance.',
    'sanctions':
      'Did any screened deployment address match the OFAC SDN list? '
      + 'Fails on any match — a sanctioned address must not be touched.',
    'sdn freshness':
      'Is the OFAC SDN list recent enough? Fails when the list is too '
      + 'stale to reflect recently sanctioned addresses.',
    'sdn list loaded':
      'Did the OFAC SDN sanctioned-address list load successfully? '
      + 'Fails if the list could not be reached or parsed.',
    'addresses screened':
      'Were the token deployment addresses screened against the SDN '
      + 'list? Fails if no address could be screened.',
    'liquid coverage':
      'Do liquid (fast-access) reserves alone cover on-chain supply? '
      + 'Fails when fast redemption capacity is below circulating supply.',
    'liquidity':
      'Is enough of the reserve held in liquid, redemption-ready assets? '
      + 'Flags reserves concentrated in slow-to-realise holdings.',
    'redemption capacity':
      'Could the issuer meet redemptions from liquid reserves? Fails when '
      + 'fast-access reserves fall short of circulating supply.',
    'net redemption flow':
      'Is the recent net redemption flow within tolerance? Flags a large '
      + 'burn of supply since the attestation date.',
  },
  // why a metric reads n/a — concise, explains it is a perceived gap, not a
  // failure, and reminds the viewer the on-chain supply read is unaffected.
  na: {
    coverage:
      'n/a — coverage needs a current reserve attestation, and the system '
      + 'could not resolve one (see GAPS below for why). NOT an on-chain '
      + 'failure: native supply is a direct chain read and is shown above.',
    reserves:
      'n/a — no current attestation document could be resolved for this '
      + 'issuer, so attested reserves are unknown. The on-chain supply figure '
      + 'is unaffected — it is read straight from the chain.',
    tokens:
      'n/a — tokens outstanding is taken from an attestation, and none could '
      + 'be resolved. On-chain supply (above) is independent and still live.',
    confidence:
      'n/a — extraction confidence scores how cleanly the LLM parsed an '
      + 'attestation PDF. With no attestation resolved there is nothing to '
      + 'score. Not an error — just nothing to extract from.',
    staleness:
      'n/a — staleness measures the age of the attestation. With no '
      + 'attestation resolved there is no date to measure against.',
    drift:
      'n/a — supply drift compares current supply to supply at the '
      + 'attestation date. With no attestation resolved there is no baseline.',
  },
  // the honest, accurate reason an attestation could not be resolved — shared
  // by every attestation-derived n/a. An issuer-transparency limit.
  noAttestation:
    'WHY — the system could not reach a current attestation. Common causes: '
    + 'the issuer publishes its attestation on a JavaScript-rendered page with '
    + 'no machine-reachable source (e.g. Paxos, TrueUSD); no transparency '
    + 'source is configured for the issuer (FDUSD); or a shared issuer page '
    + 'carried no token-specific report (EURC). This is an issuer-transparency '
    + 'limit — the tool honestly reports what it can reach. On-chain supply is '
    + 'a direct chain read and is unaffected.',
  // per-category plain-language "why" for a GAPS & OPEN ITEMS row. Each notes
  // whether it is an issuer-transparency limit or an action the operator can
  // take. Keyed by the gap's `category` field.
  gapWhy: {
    data: {
      kind: 'issuer-transparency limit',
      text: 'The system could not resolve a current attestation document — '
        + 'the issuer publishes it on a JavaScript-rendered page with no '
        + 'machine-reachable source, has no transparency source configured, or '
        + 'the shared issuer page held no token-specific report. The on-chain '
        + 'supply read is unaffected; only attestation-derived figures are.',
    },
    corpus: {
      kind: 'awaiting your action',
      text: 'No ingested source text in the corpus yet. Sources are citable '
        + 'by default, but a source only carries a citation once its text is '
        + 'staged and ingested. Stage text under corpus/staging/ to let '
        + 'judgements cite regulation.',
    },
    coverage: {
      kind: 'pending verification',
      text: 'A contract address hasn\'t passed on-chain identity verification ' +
        'yet. Its figure is included but flagged as provisional. The ' +
        'background canary attempts to auto-verify every six hours by calling ' +
        'the contract\'s own symbol() and decimals(); a curator can also ' +
        'mark it verified directly from the Corpus view.',
    },
    guardrail: {
      kind: 'honest disclosure',
      text: 'A deterministic guardrail surfaced something worth noting — e.g. '
        + 'an attestation that is real but dated, because a fresher one is not '
        + 'machine-reachable. The staleness is reported honestly, not hidden.',
    },
    citation: {
      kind: 'pending verification',
      text: 'A judgement cited a source outside the included corpus. Only ' +
        'included sources are accepted as citations — re-include the ' +
        'source from the Corpus view, or stage its text, to let the claim ' +
        'carry a citation.',
    },
  },
  // ── F5 sanctions / F6 redemptions surface terms ────────────────────
  sdnStaleness:
    'SDN staleness — days elapsed since the OFAC Specially Designated '
    + 'Nationals list was last published. An older list may miss recently '
    + 'sanctioned addresses, so a stale list weakens the screen.',
  sdnCount:
    'SDN address count — total digital-currency addresses on the OFAC SDN '
    + 'list this screen was run against. The size of the sanctioned-address '
    + 'universe checked.',
  screened:
    'Screened addresses — the token deployment contract addresses checked '
    + 'against every sanctioned address on the OFAC SDN list.',
  liquidReserves:
    'Liquid reserves — the portion of reserves held in assets that can fund '
    + 'redemptions immediately (cash, overnight repo, T-bills). Excludes '
    + 'slower-to-realise holdings.',
  liquidCoverage:
    'Liquid coverage — liquid reserves ÷ current on-chain supply. How much '
    + 'of circulating supply could be redeemed using only fast-access assets, '
    + 'without selling slower holdings.',
  netRedemptionFlow:
    'Net redemption flow — the change in on-chain supply since the '
    + 'attestation date. Negative = net redemptions (supply burned); '
    + 'positive = net issuance (supply minted).',
  tier:
    'Liquidity tier — how fast a reserve line can fund redemptions. '
    + 'liquid = immediate (cash, T-bills); moderate = days (longer repo, '
    + 'corporate paper); illiquid = slow to realise.',
  // operations-log column legend — one concise line per field.
  log: {
    time:    'Time — local clock time the event was logged.',
    level:   'Level — OK (clean), WATCH (warnings raised), ERR (read failed), '
      + 'WORK (agent task in progress), INFO (operator note).',
    event:   'Event — which operation produced the line (SUPPLY read, ANALYZE '
      + 'job, REGISTRY load, etc.).',
    token:   'Token — the stablecoin instrument this line is about.',
    supply:  'Supply — current native on-chain supply, compact magnitude '
      + '(B = billion, M = million).',
    chains:  'Chains — number of chain deployments the supply was summed across.',
    delta:   'Δ change — measured percentage change in supply since the previous '
      + 'real read. "flat" means no material move.',
    bridged: 'Bridged — supply held as wrapped/bridged copies, excluded from the '
      + 'native headline. 0 means none.',
    verified:'Verified — source-verified deployments out of total. 6/6 is fully '
      + 'verified; anything lower is read with lower confidence.',
    warnings:'Warnings — count of agent warnings raised on this read. Blank when '
      + 'there are none.',
    detail:  'Detail — free-text status for non-SUPPLY events (job stage, error '
      + 'message, counts).',
  },
};
// best-effort match a guardrail check name to a tooltip
function checkTip(name) {
  const key = String(name || '').toLowerCase().trim();
  if (tip.checks[key]) return tip.checks[key];
  for (const k of Object.keys(tip.checks)) {
    if (key.includes(k) || k.includes(key)) return tip.checks[k];
  }
  return 'Deterministic guardrail check — a pass/fail rule run against the '
    + 'extracted facts before any LLM synthesis.';
}

// ── plain-language presentation layer ────────────────────────────────
// The underlying data carries engineer identifiers (technical guardrail
// check ids, inline [tool:X] provenance markers). The package keeps them
// — a citation-verification guardrail depends on them. These maps live
// at the DISPLAY layer only: they translate those identifiers into plain
// language a compliance officer or financial analyst reads. The raw
// values are never mutated, only re-labelled at render time.

// every guardrail check id enumerated from src/sca/validation.py →
// a clear, audience-appropriate label.
const CHECK_LABELS = {
  // attestation guardrails
  reserves_positive: 'Reserves are positive',
  tokens_positive: 'Tokens outstanding are positive',
  breakdown_sums_to_total: 'Reserve breakdown reconciles to total',
  attestation_date_valid: 'Attestation date valid',
  extraction_confidence: 'Attestation read with confidence',
  // metrics guardrail
  coverage_plausible: 'Coverage ratio plausible',
  // supply guardrail
  supply_resolved: 'Supply confirmed',
  // sanctions guardrails
  sdn_list_loaded: 'Sanctions list loaded',
  sdn_list_fresh: 'Sanctions list current',
  no_sanctioned_addresses: 'No sanctioned addresses',
  // redemption guardrails
  liquid_coverage_plausible: 'Liquid coverage plausible',
  reserve_classification_complete: 'Reserve breakdown classified',
  // citation-verification guardrails
  citations_tool_valid: 'Sources verified',
  citations_source_valid: 'Regulatory citations verified',
  figures_traceable: 'Figures traced to data',
};

// humanise any check name not in the map — underscores to spaces,
// sentence-cased — so an unmapped id never reaches the audience raw.
function checkLabel(name) {
  const raw = String(name || '').trim();
  if (!raw) return 'Guardrail check';
  if (CHECK_LABELS[raw]) return CHECK_LABELS[raw];
  const key = raw.toLowerCase();
  if (CHECK_LABELS[key]) return CHECK_LABELS[key];
  const words = raw.replace(/[_-]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// inline [tool:X] provenance markers → plain-language provenance label.
const TOOL_PROVENANCE = {
  onchain_supply: 'on-chain data',
  attestation_extract: 'the attestation',
  attestation_fetch: 'the attestation',
  metrics: 'calculated',
  sanctions: 'OFAC SDN list',
  redemption: 'reserve analysis',
};
function toolProvenance(id) {
  const key = String(id || '').toLowerCase().trim();
  return TOOL_PROVENANCE[key]
    || key.replace(/[_-]+/g, ' ').trim()
    || 'source data';
}

// a corpus citation slug ([mica-title-iii §Article 36]) → clean source
// label. Compliance audiences want regulatory citations — kept, just
// rendered legibly: the registry slug becomes a recognisable name and
// the section is joined with an en dash.
const CORPUS_SOURCE_NAMES = {
  'mica-title-iii': 'MiCA',
  'mica-title-iv': 'MiCA',
  mica: 'MiCA',
  'gerie-act': 'GENIUS Act',
  genius: 'GENIUS Act',
  'genius-act': 'GENIUS Act',
};
function corpusSourceName(slug) {
  const key = String(slug || '').toLowerCase().trim();
  if (CORPUS_SOURCE_NAMES[key]) return CORPUS_SOURCE_NAMES[key];
  // strip a trailing "-title-iii" style qualifier, then title-case
  const base = key.replace(/-title-[ivx]+$/i, '');
  if (CORPUS_SOURCE_NAMES[base]) return CORPUS_SOURCE_NAMES[base];
  return base.replace(/[_-]+/g, ' ').replace(/\b\w/g,
    (c) => c.toUpperCase());
}

// tidy a check-detail string: drop technical key=value phrasing for plain
// text. Light touch — leaves prose details untouched.
function cleanDetail(detail) {
  let s = String(detail || '');
  if (!s) return s;
  // total_supply=73,410,450,793  →  Total supply: 73,410,450,793
  s = s.replace(/\b([a-z][a-z0-9_]*)\s*=\s*/gi, (_, key) => {
    const words = key.replace(/[_-]+/g, ' ').trim();
    return words.charAt(0).toUpperCase() + words.slice(1) + ': ';
  });
  // drop a leading "tool" word should it ever appear in a detail string
  s = s.replace(/\btool outputs\b/gi, 'the source data')
       .replace(/\btool citations?\b/gi, 'source references');
  return s;
}

// strip a leading "technical_check_name:" prefix from a gap message and
// replace it with the plain check label.
function cleanGapMessage(message) {
  const s = String(message || '');
  const m = s.match(/^\s*([a-z][a-z0-9_]*)\s*:\s*([\s\S]*)$/i);
  if (m && (CHECK_LABELS[m[1]] || /_/.test(m[1]))) {
    return checkLabel(m[1]) + ' — ' + cleanDetail(m[2]);
  }
  return cleanDetail(s);
}

// resolve the plain-language "why" for a gap, keyed by its category. Returns
// {kind, text} — kind distinguishes an issuer-transparency limit from an
// action the operator can take, so the gap reads as honest transparency.
function gapWhy(category) {
  const key = String(category || 'data').toLowerCase().trim();
  return tip.gapWhy[key] || {
    kind: 'open item',
    text: 'The agent reached what it could and is reporting this honestly as '
      + 'an open item rather than omitting it.',
  };
}

// ── the operations feed — dense but legible, column-structured ───────
// Every row shares the same labelled column grid (see .log-line CSS and
// the LOG-HEADER row): time · level · event · then data fields. A SUPPLY
// row fills the named columns (token·supply·chains·Δ·bridged·verified·
// warnings); other events fill a single wide DETAIL column instead.
let _feedId = 0;
function logLine(level, tag, data, detail) {
  STATE.feed.push({
    _id: ++_feedId, time: clockStr(), level, tag, data, detail: detail || null,
  });
  if (STATE.feed.length > FEED_MAX) STATE.feed.shift();
  STATE.feedSeq++;
  renderFeedTail();
  const ln = $('#ts-lines');
  if (ln) ln.textContent = STATE.feedSeq;
}
// a data segment — text + optional colour class. Used for the free-text
// DETAIL column on non-SUPPLY events.
const seg = (t, c = '') => ({ t: String(t), c });

// a fully column-structured SUPPLY line — values land in fixed,
// header-labelled columns so nothing ever runs together. `snap` carries
// the full event payload (per-chain rows, warnings, read timestamp, the
// previous native read) so the click-to-expand detail panel can show the
// real data behind the row without re-fetching or fabricating.
function supplyLine(level, sym, native, chains, delta, bridged, verified, warn, snap) {
  STATE.feed.push({
    _id: ++_feedId, time: clockStr(), level, tag: 'SUPPLY', kind: 'supply',
    sym, native, chains, delta, bridged, verified, warn,
    snap: snap || null,
  });
  if (STATE.feed.length > FEED_MAX) STATE.feed.shift();
  STATE.feedSeq++;
  renderFeedTail();
  const ln = $('#ts-lines');
  if (ln) ln.textContent = STATE.feedSeq;
}

// one fixed cell in a log row — class names the column for CSS alignment
function cell(col, text, extra = '', title = '') {
  return el('span',
    { class: ('lg-c lg-' + col + (extra ? ' ' + extra : '')).trim(),
      title: title || null },
    text);
}

// the labelled column header that sits above the feed
function feedHeaderRow() {
  const t = tip.log;
  return el('div', { class: 'log-line log-head' },
    cell('time', 'TIME', '', t.time),
    cell('lvl', 'LVL', '', t.level),
    cell('tag', 'EVENT', '', t.event),
    cell('sym', 'TOKEN', '', t.token),
    cell('val', 'SUPPLY', '', t.supply),
    cell('chains', 'CHAINS', '', t.chains),
    cell('delta', 'Δ CHANGE', '', t.delta),
    cell('brdg', 'BRIDGED', '', t.bridged),
    cell('vfy', 'VERIFIED', '', t.verified),
    cell('warn', 'WARNINGS', '', t.warnings),
  );
}

function feedRowNode(l) {
  const t = tip.log;
  const row = el('div', {
    class: 'log-line log-click' + (l._open ? ' log-open' : ''),
    'data-feed-id': l._id,
    title: 'click to ' + (l._open ? 'collapse' : 'expand') + ' — event detail',
    onclick: (e) => {
      // never hijack a click on a link inside the row (e.g. an addr-link)
      if (e.target.closest && e.target.closest('a')) return;
      toggleFeedDetail(l);
    },
  },
    cell('time', l.time, '', t.time),
    cell('lvl', l.level, 'lvl-' + l.level, tip.log.level),
    cell('tag', l.tag || '', '', t.event),
  );
  if (l.kind === 'supply') {
    const d = l.delta;
    const dTxt = (d == null || Math.abs(d * 100) < 0.005)
      ? 'flat'
      : (d > 0 ? '+' : '') + (d * 100).toFixed(2) + '%';
    row.append(
      cell('sym', l.sym, 'lg-sym', t.token),
      cell('val', fmtMag(l.native), 'lg-val', t.supply),
      cell('chains', l.chains + ' chains', '', t.chains),
      cell('delta', 'Δ ' + dTxt, deltaClass(d), t.delta),
      cell('brdg', l.bridged > 0 ? fmtMag(l.bridged) : '0',
        l.bridged > 0 ? 'd-warn' : 'v-muted', t.bridged),
      cell('vfy', l.verified + '/' + l.chains,
        l.verified === l.chains ? 'd-up' : 'd-warn', t.verified),
      cell('warn', l.warn ? l.warn + ' warning' + (l.warn > 1 ? 's' : '') : '',
        l.warn ? 'd-warn' : '', t.warnings),
    );
  } else {
    // non-SUPPLY event — render the free-text segments in one wide column
    const detail = el('span', { class: 'lg-c lg-detail', title: t.detail });
    const data = l.data;
    if (typeof data === 'string') {
      detail.append(document.createTextNode(data));
    } else if (Array.isArray(data)) {
      data.forEach((dseg, i) => {
        if (dseg == null) return;
        if (i) detail.append(document.createTextNode('  ·  '));
        detail.append(el('span', { class: (dseg.c || '').trim() || null }, dseg.t));
      });
    }
    row.append(detail);
  }
  return row;
}

// ── click-to-expand detail panel ─────────────────────────────────────
// An inline, terminal-grade panel rendered directly beneath a feed row.
// Every figure is read from the row's OWN captured event data — nothing
// is re-fetched or fabricated. Tailored per event type.

// a labelled key/value line inside a detail panel
function dKV(k, v, cls = '') {
  return el('div', { class: 'ld-kv' },
    el('span', { class: 'ld-k' }, k),
    el('span', { class: 'ld-v ' + cls }, v));
}
// a section caption inside a detail panel
function dCap(text) { return el('div', { class: 'ld-cap' }, text); }
// the one-line plain-language explainer strip
function dNote(text) { return el('div', { class: 'ld-note' }, text); }

function feedDetailPanel(l) {
  const panel = el('div', { class: 'log-detail-panel' });
  const box = el('div', { class: 'ld-box' });
  panel.append(box);

  if (l.kind === 'supply') {
    buildSupplyDetail(l, box);
  } else {
    buildEventDetail(l, box);
  }
  return panel;
}

// SUPPLY rows — the richest case: per-chain breakdown, read timestamp,
// measured Δ vs the previous read, bridged figure, warnings, plus a
// one-line plain explanation and a jump to the full F2 analysis.
function buildSupplyDetail(l, box) {
  const s = l.snap || {};
  const chains = s.per_chain || [];

  box.append(dNote('A SUPPLY read sums the token’s on-chain circulating '
    + 'supply directly from each chain’s contract — a deterministic chain '
    + 'read, taken fresh every poll. The headline counts native issuance only.'));

  // context strip
  const ctx = el('div', { class: 'ld-strip' });
  ctx.append(dKV('TOKEN', l.sym, 'v-gold'));
  ctx.append(dKV('NATIVE SUPPLY', fmtNum(l.native, 0)));
  ctx.append(dKV('GROSS (incl. bridged)',
    fmtNum((l.native || 0) + (l.bridged || 0), 0)));
  ctx.append(dKV('BRIDGED', l.bridged > 0 ? fmtNum(l.bridged, 0) : 'none',
    l.bridged > 0 ? 'v-amber' : 'v-muted'));
  // measured delta vs the previous real read
  if (s.prev_native != null) {
    const abs = (l.native || 0) - s.prev_native;
    const dCls = abs > 0 ? 'd-up' : abs < 0 ? 'd-dn' : 'v-muted';
    ctx.append(dKV('Δ VS PREVIOUS READ',
      (abs >= 0 ? '+' : '') + fmtNum(abs, 0) + '  (' + fmtDelta(l.delta) + ')',
      dCls));
  } else {
    ctx.append(dKV('Δ VS PREVIOUS READ', 'first read this session — no baseline',
      'v-muted'));
  }
  ctx.append(dKV('VERIFIED DEPLOYMENTS', l.verified + ' / ' + l.chains,
    l.verified === l.chains ? 'd-up' : 'd-warn'));
  ctx.append(dKV('READ TIMESTAMP', s.read_at || l.time, 'v-muted'));
  box.append(ctx);

  // per-chain breakdown
  if (chains.length) {
    box.append(dCap('PER-CHAIN BREAKDOWN · ' + chains.length + ' deployment(s)'));
    const rows = chains.map((c) => {
      const isBridged = c.kind === 'bridged';
      const verified = c.verified !== false;
      const expUrl = explorerUrl(c.chain, c.contract);
      const addrCell = expUrl
        ? el('a', { class: 'addr addr-link', href: expUrl,
            target: '_blank', rel: 'noopener noreferrer',
            title: 'open contract on ' + c.chain + ' explorer' },
            el('span', {}, c.contract), el('span', { class: 'addr-ext' }, '↗'))
        : el('span', { class: 'addr' }, c.contract);
      return el('tr', {},
        el('td', {}, el('span', { class: 'chain-id' },
          chainMark(c.chain, 'cmark-tbl'), c.chain)),
        el('td', {}, addrCell),
        el('td', {}, el('span', {
          class: 'kind kind-' + (isBridged ? 'bridged' : 'native') },
          isBridged ? 'bridged' : 'native')),
        el('td', {}, el('span', {
          class: 'vmark ' + (verified ? 'vmark-ok' : 'vmark-no'),
          title: verified ? tip.verified : tip.unverified },
          icon(verified ? 'i-ok' : 'i-warn'),
          verified ? 'verified' : 'unverified')),
        el('td', { class: 'num ' + (isBridged ? 'v-amber' : 'v-paper') },
          fmtNum(c.supply, 0)));
    });
    box.append(el('table', { class: 'dtable ld-table' },
      el('thead', {}, el('tr', {},
        el('th', {}, 'CHAIN'), el('th', {}, 'CONTRACT'),
        el('th', {}, 'KIND'), el('th', {}, 'VERIFIED'),
        el('th', { class: 'num' }, 'SUPPLY'))),
      el('tbody', {}, ...rows)));
  } else {
    box.append(dNote('No per-chain breakdown was captured for this read.'));
  }

  // warnings
  const warns = s.warnings || [];
  if (warns.length) {
    box.append(dCap('WARNINGS · ' + warns.length));
    warns.forEach((w) => box.append(
      el('div', { class: 'ld-warn' }, icon('i-warn'), el('span', {}, w))));
  } else {
    box.append(dCap('WARNINGS'));
    box.append(el('div', { class: 'ld-clean' },
      icon('i-ok'), el('span', {}, 'No warnings — read resolved cleanly.')));
  }

  // cross-surface linkage — jump to the full F2 analysis for this token
  box.append(el('div', { class: 'ld-actions' },
    el('a', { class: 'ld-link', href: '#analyze/' + l.sym,
      title: 'open the full reserve analysis for ' + l.sym + ' in F2 Analyze' },
      el('span', { class: 'glyph' }, '→'),
      el('span', {}, 'open full analysis · F2 ' + l.sym))));
}

// non-SUPPLY events — ANALYZE stage rows, BOOT / REGISTRY / OK / ERR / MON.
// Show the full untruncated message + a plain explanation of the event.
function buildEventDetail(l, box) {
  // full, untruncated event text reassembled from the row's data segments
  const fullText = (() => {
    if (typeof l.data === 'string') return l.data;
    if (Array.isArray(l.data)) {
      return l.data.filter(Boolean).map((d) => d.t).join('  ·  ');
    }
    return '';
  })();

  // an ANALYZE pipeline stage row — "stage N/M …"
  let stageMatch = null;
  if (Array.isArray(l.data)) {
    for (const d of l.data) {
      const mm = d && /^stage\s+(\d+)\/(\d+)/.exec(String(d.t).trim());
      if (mm) { stageMatch = mm; break; }
    }
  }

  const explainer = eventExplainer(l, stageMatch);
  if (explainer) box.append(dNote(explainer));

  if (stageMatch) {
    box.append(el('div', { class: 'ld-strip' },
      dKV('PIPELINE', 'ANALYZE · reserve reconciliation', 'v-gold'),
      dKV('STAGE', stageMatch[1] + ' of ' + stageMatch[2]),
      dKV('PROGRESS',
        Math.round((Number(stageMatch[1]) / Number(stageMatch[2])) * 100) + '%')));
  }

  box.append(dCap('FULL EVENT'));
  box.append(el('div', { class: 'ld-fulltext' },
    el('span', { class: 'ld-stamp' }, l.time + '  ' + l.level + '  ' + (l.tag || '')),
    el('div', {}, fullText || '(no further detail)')));

  // surface any explicitly attached detail payload verbatim
  if (l.detail) {
    box.append(dCap('CONTEXT'));
    box.append(el('div', { class: 'ld-fulltext' }, String(l.detail)));
  }
}

// a plain-language explanation tailored to the event type. Honest: if the
// event carries little, it says so rather than inventing context.
function eventExplainer(l, stageMatch) {
  const tag = String(l.tag || '').toUpperCase();
  if (stageMatch) {
    const stages = {
      '1': 'reading live on-chain supply from every chain deployment.',
      '2': 'resolving and extracting the latest reserve attestation.',
      '3': 'running deterministic guardrail checks against the facts.',
      '4': 'retrieving corpus passages for the reasoning frame.',
      '5': 'synthesising the cited analysis narrative.',
    };
    const n = stageMatch[1];
    return 'ANALYZE pipeline — stage ' + n + '/' + stageMatch[2] + ': '
      + (stages[n] || 'a step of the reserve-reconciliation pipeline is running.')
      + ' Each stage runs server-side; the feed narrates progress as it advances.';
  }
  if (tag === 'MON') {
    return 'The monitor loop is armed — it re-reads on-chain supply for every '
      + 'instrument on a fixed cadence. Values are held at their last real '
      + 'read between polls, never simulated.';
  }
  if (tag === 'BOOT' || tag === 'REGISTRY') {
    return 'A startup event — the terminal loaded its instrument registry and '
      + 'brought the live monitor online.';
  }
  if (tag === 'ANALYZE') {
    if (l.level === 'OK') return 'An ANALYZE job finished — the figures shown '
      + 'are the headline coverage results; open F2 Analyze for the full report.';
    if (l.level === 'ERR') return 'An ANALYZE job failed — the full error text '
      + 'is shown below; the on-chain supply reads are unaffected.';
    return 'An ANALYZE job event — a reserve-reconciliation run for this token.';
  }
  if (l.level === 'ERR') {
    return 'An error event — the full message is shown below untruncated so the '
      + 'cause is legible.';
  }
  if (l.level === 'OK') {
    return 'A completed operation — the full result line is shown below.';
  }
  return 'Event detail — the full, untruncated log line for this event.';
}

// prepend the newest line(s) when the feed is mounted — reverse-chronological:
// the newest line sits at the top, the feed grows downward into older entries.
// A row may be followed by an inline .log-detail-panel sibling when expanded;
// the rolling-buffer trim drops a row together with its detail panel.
let _feedRendered = 0;
function renderFeedTail() {
  const feed = $('#op-feed');
  if (!feed) { _feedRendered = STATE.feed.length; return; }
  if (_feedRendered > STATE.feed.length) _feedRendered = 0; // feed trimmed
  // viewing the top means following the live edge — pin back to it after insert
  const atTop = feed.scrollTop <= 24;
  // insert each new line at the top, oldest-of-the-new first so order holds
  for (let i = _feedRendered; i < STATE.feed.length; i++) {
    feed.insertBefore(feedRowNode(STATE.feed[i]), feed.firstChild);
  }
  // drop overflow rows from the bottom (oldest) to match the rolling buffer —
  // a trimmed row takes its detail panel (if any) with it.
  while (feed.querySelectorAll('.log-line').length > STATE.feed.length) {
    let last = feed.lastChild;
    while (last && last.classList && last.classList.contains('log-detail-panel')) {
      const prev = last.previousSibling;
      feed.removeChild(last);
      last = prev;
    }
    if (last) feed.removeChild(last);
  }
  _feedRendered = STATE.feed.length;
  if (atTop) feed.scrollTop = 0;
}

// expand / collapse the inline detail panel beneath a feed row. Keeps the
// row in place — the panel is a sibling inserted directly after the row.
function toggleFeedDetail(l) {
  const feed = $('#op-feed');
  if (!feed) return;
  const row = feed.querySelector('.log-line[data-feed-id="' + l._id + '"]');
  if (!row) return;
  if (l._open) {
    l._open = false;
    row.classList.remove('log-open');
    row.setAttribute('title', 'click to expand — event detail');
    const panel = row.nextSibling;
    if (panel && panel.classList && panel.classList.contains('log-detail-panel')) {
      feed.removeChild(panel);
    }
    return;
  }
  l._open = true;
  row.classList.add('log-open');
  row.setAttribute('title', 'click to collapse — event detail');
  const panel = feedDetailPanel(l);
  feed.insertBefore(panel, row.nextSibling);
}

// ── the live monitor loop ────────────────────────────────────────────
// One cadence keeps the monitor alive — and it only ever moves on REAL
// data:
//   • pollTick  — re-reads /api/supply/{sym} (server TTL-cached ~60s, so
//     this is cheap), cycling one instrument per call. Emits a dense
//     SUPPLY line with the real native figure + delta vs the last read.
// Between real reads every displayed value is HELD at its last real
// value — never walked, jittered, or estimated. Every figure on screen
// traces to a real /api/supply or /api/analyze response.
let _monitorTimer = null;
let _monitorIdx = 0;
STATE.anchor = {};  // symbol -> last real native value from a server read
STATE.realDelta = {};  // symbol -> real change between the last two reads
STATE.readAt = {};  // symbol -> clock time of the last real read

function nativeOf(s) {
  return s && s !== 'error' ? Number(s.native_supply || s.total_supply || 0) : null;
}

async function pollTick() {
  if (!STATE.tokens.length) return;
  const tok = STATE.tokens[_monitorIdx % STATE.tokens.length];
  _monitorIdx++;
  const sym = tok.symbol;
  try {
    const s = await api('/supply/' + sym);
    const prevAnchor = STATE.anchor[sym];
    STATE.supply[sym] = s;
    const native = nativeOf(s);
    const bridged = Number(s.bridged_supply || 0);
    const chains = (s.per_chain || []).length;
    const verified = (s.per_chain || []).filter((c) => c.verified !== false).length;
    const warn = (s.warnings || []).length;
    // delta vs last real read — a real measured change, not a simulation
    const delta = prevAnchor ? (native - prevAnchor) / prevAnchor : null;
    STATE.anchor[sym] = native;
    STATE.realDelta[sym] = delta;
    STATE.readAt[sym] = clockStr();
    const level = warn ? 'WATCH' : 'OK';
    // snapshot the full read so the click-to-expand detail panel shows the
    // real per-chain breakdown / warnings / timestamps for THIS line, even
    // after later polls overwrite STATE.supply[sym].
    supplyLine(level, sym, native, chains, delta, bridged, verified, warn, {
      per_chain: (s.per_chain || []).map((c) => ({ ...c })),
      warnings: (s.warnings || []).slice(),
      read_at: s.read_at || null,
      total_supply: Number(s.total_supply || 0),
      native, bridged,
      prev_native: prevAnchor != null ? prevAnchor : null,
    });
    refreshGrid();
    refreshInstruments();
    refreshTicker();
  } catch (e) {
    // The supply endpoint is cache-dominated server-side; a failure here
    // is a real outage (server down, network drop) — not RPC flap. The
    // label reads honestly to the financial audience, with the raw error
    // shown for ops in the tail segment.
    STATE.supply[sym] = 'error';
    logLine('WATCH', 'SUPPLY', [
      seg(sym.padEnd(5), 'lg-sym'),
      seg('read deferred', 'd-warn'),
      seg(String(e.message).slice(0, 48)),
    ]);
    refreshGrid();
    refreshInstruments();
    refreshTicker();
  }
}

function startMonitor() {
  if (STATE.monitorRunning) return;
  STATE.monitorRunning = true;
  logLine('WORK', 'MON', [
    seg('loop armed', 'lg-val'),
    seg(STATE.tokens.length + ' instr'),
    seg('poll 3.4s'),
    seg('values held between reads'),
  ]);
  pollTick();
  _monitorTimer = setInterval(pollTick, 3400);
}

// ── live monitor grid (token table) ──────────────────────────────────
// Status labels carry a tooltip explaining what each means — so a viewer
// can hover and know whether WATCH is a polite annotation or something
// actually warranting investigation.
function tokenStatus(s) {
  if (s === 'error') {
    return { tag: 'alert', label: 'ALERT',
      tip: 'The on-chain supply read failed entirely for this token. '
        + 'No figures available until the next poll succeeds. '
        + 'Check chain RPC health via `sca canary`.' };
  }
  if (!s) {
    return { tag: 'idle', label: 'PENDING',
      tip: 'No supply read yet — the monitor cycles through every '
        + 'instrument; this one hasn\'t been polled in this session.' };
  }
  if ((s.warnings || []).length) {
    const partial = s.complete === false;
    return { tag: 'watch', label: 'WATCH',
      tip: partial
        ? `Partial read — ${s.chains_read}/${s.chains_expected} chains `
          + `returned. Headline figure understates true circulation. `
          + `Failed: ${(s.failed_chains || []).join(', ') || '?'}.`
        : `Supply read succeeded but with ${s.warnings.length} warning(s) — `
          + `usually an unverified contract address or a supply jump. `
          + `Hover the row for detail; not necessarily a problem.` };
  }
  return { tag: 'ok', label: 'LIVE',
    tip: 'On-chain supply read succeeded with no warnings across every '
      + 'expected chain. Multi-RPC corroboration where available.' };
}

function refreshGrid() {
  const tbody = $('#grid-rows');
  if (!tbody) return;
  tbody.innerHTML = '';
  STATE.tokens.forEach((t) => {
    const s = STATE.supply[t.symbol];
    const st = tokenStatus(s);
    // displayed value is the real on-chain read — held static between polls
    const native = nativeOf(s);
    // real measured change between the last two real reads (null until 2 reads)
    const delta = STATE.realDelta[t.symbol] != null ? STATE.realDelta[t.symbol] : null;
    const readAt = STATE.readAt[t.symbol] || null;
    const bridged = s && s !== 'error' ? Number(s.bridged_supply || 0) : null;
    const gross = native != null ? native + (bridged || 0) : null;
    const bShare = gross ? bridged / gross : null;
    const chains = s && s !== 'error' ? (s.per_chain || []).length : null;
    const verified = t.verified_count === t.chain_count;
    tbody.append(el('tr', {
      class: 'click',
      onclick: () => { location.hash = currentSurfaceHash(t.symbol); },
    },
      el('td', { class: 'td-mark' }, tokenMark(t.symbol, 'tmark-grid')),
      el('td', { class: 'sym' }, t.symbol),
      el('td', { class: 'dim' }, t.issuer),
      el('td', { class: 'num ' + (native == null ? 'v-muted' : 'v-paper'),
        title: readAt ? 'on-chain read as of ' + readAt : null },
        native == null ? '—' : fmtUSD(native)),
      el('td', { class: 'num ' + (deltaClass(delta) || 'v-muted'),
        title: tip.drift },
        delta == null ? '—' : fmtDelta(delta)),
      el('td', { class: 'num v-muted' }, chains == null ? '—' : chains),
      el('td', { class: 'num ' + (bShare ? 'v-amber' : 'v-muted'),
        title: tip.bridged },
        bShare == null ? '—' : fmtPct(bShare)),
      el('td', { class: 'num ' + (verified ? 'v-green' : 'v-amber'),
        title: tip.verifiedRatio },
        `${t.verified_count}/${t.chain_count}`),
      el('td', {}, el('span', { class: 'stag ' + st.tag,
        title: st.tip || tip.status[st.tag] },
        st.label)),
      el('td', {}, el('div', { style: 'display:flex;gap:8px' },
        el('a', { class: 'cite', href: '#sanctions/' + t.symbol,
          title: 'OFAC sanctions screen for ' + t.symbol,
          onclick: (e) => { e.stopPropagation(); } },
          el('span', { class: 'glyph' }, 'F5'), el('span', {}, 'screen')),
        el('a', { class: 'cite', href: '#redemptions/' + t.symbol,
          title: 'redemption-capacity assessment for ' + t.symbol,
          onclick: (e) => { e.stopPropagation(); } },
          el('span', { class: 'glyph' }, 'F6'), el('span', {}, 'redeem')))),
    ));
  });
}

// ── live ticker tape ─────────────────────────────────────────────────
// Persistent marquee under the status bar. Numbers come straight from the
// same STATE.supply / STATE.realDelta the monitor poll populates — one
// source of truth, no invented or random-walked values. The horizontal
// scroll is cosmetic CSS; the figures update only on a real poll and are
// held static between reads.
function tickItem(t) {
  const s = STATE.supply[t.symbol];
  const native = nativeOf(s);
  const isErr = s === 'error';
  const pending = native == null;
  // real measured change between the last two real reads — never simulated
  const delta = STATE.realDelta[t.symbol] != null
    ? STATE.realDelta[t.symbol] : null;
  const dCls = pending ? 'd-flat' : (deltaClass(delta) || 'd-flat');
  // directional mark on the REAL delta — ▲ genuine rise, ▼ genuine fall.
  // The flat state gets a designed hollow diamond, not a dead dash.
  const arrow = dCls === 'd-up' ? '▲' : dCls === 'd-dn' ? '▼' : '◇';
  // absolute supply move, for the dense secondary figure
  const absMove = (delta != null && native != null)
    ? native * delta : null;
  const item = el('span', {
    class: 'tick-item' + (isErr ? ' tick-err' : '')
      + (pending ? ' tick-pending' : ''),
  },
    // brand-coloured token mark — always present, carries colour even
    // when supply is flat. Reuses the tokenMark() sprite + data-token.
    tokenMark(t.symbol, 'tmark-tick'),
    // the symbol carries its brand colour too — data-token drives the
    // per-token hue in CSS, so every item is vivid by brand identity.
    el('span', { class: 'tick-sym', 'data-token': t.symbol }, t.symbol),
    el('span', { class: 'tick-val' }, pending ? '— —' : fmtUSD(native)),
  );
  if (isErr) {
    item.append(el('span', { class: 'tick-delta d-flat' },
      el('span', { class: 'tick-arrow' }, '×'),
      el('span', { class: 'tick-pct' }, 'OFFLINE')));
  } else if (pending) {
    item.append(el('span', { class: 'tick-delta d-flat' },
      el('span', { class: 'tick-arrow' }, '◇'),
      el('span', { class: 'tick-pct' }, 'LOADING')));
  } else {
    // a flat reading shows a calm, deliberate "FLAT" label; a real move
    // shows the signed percentage.
    const dTxt = dCls === 'd-flat' ? 'FLAT' : fmtDelta(delta);
    const dWrap = el('span', { class: 'tick-delta ' + dCls },
      el('span', { class: 'tick-arrow' }, arrow),
      el('span', { class: 'tick-pct' }, dTxt));
    // show the honest absolute Δ alongside the % when there is real motion
    if (dCls !== 'd-flat' && absMove != null) {
      dWrap.append(el('span', { class: 'tick-abs' },
        (absMove >= 0 ? '+' : '−') + fmtMag(Math.abs(absMove))));
    }
    item.append(dWrap);
  }
  return item;
}

function refreshTicker() {
  const track = $('#ticker-track');
  if (!track) return;
  if (!STATE.tokens.length) {
    track.innerHTML = '<span class="tick-empty">Loading on-chain reads…</span>';
    return;
  }
  track.innerHTML = '';
  // two identical passes — the CSS marquee translates -50% for a seamless loop
  for (let pass = 0; pass < 2; pass++) {
    STATE.tokens.forEach((t) => track.append(tickItem(t)));
  }
}

function refreshInstruments() {
  const box = $('#side-instr');
  if (!box) return;
  box.innerHTML = '';
  STATE.tokens.forEach((t) => {
    const s = STATE.supply[t.symbol];
    const st = tokenStatus(s);
    // real on-chain read — held static between polls
    const native = nativeOf(s);
    box.append(el('div', {
      class: 'instr-row' + (t.symbol === STATE.activeSymbol ? ' active' : ''),
      onclick: () => { location.hash = currentSurfaceHash(t.symbol); },
    },
      el('div', { class: 'instr-id' },
        tokenMark(t.symbol, 'tmark-side'),
        el('div', {},
          el('div', { class: 'instr-sym' }, t.symbol),
          el('div', { class: 'instr-sub' }, t.issuer))),
      el('div', {},
        el('div', { class: 'instr-val ' + (native == null ? 'v-muted' : '') },
          native == null ? '— —' : fmtUSD(native)),
        el('div', { class: 'instr-stat v-' + ({ ok: 'green', watch: 'amber', alert: 'rose', idle: 'muted' }[st.tag]) },
          st.label)),
    ));
  });
  const c = $('#instr-count');
  if (c) c.textContent = STATE.tokens.length;
}

// ════════════════════════════════════════════════════════════════════
//  STATUS BAR
// ════════════════════════════════════════════════════════════════════
function tickClock() {
  const c = $('#ts-clock');
  if (c) c.textContent = clockStr();
}
async function pollHealth() {
  const dot = $('#ts-dot'), conn = $('#ts-conn'), llm = $('#ts-llm'), tk = $('#ts-tokens');
  try {
    const h = await api('/health');
    dot.className = 'dot live';
    conn.textContent = 'ONLINE';
    llm.textContent = h.llm_configured ? 'READY' : 'NO-KEY';
    llm.style.color = h.llm_configured ? 'var(--green)' : 'var(--amber)';
    tk.textContent = h.tokens;
  } catch {
    dot.className = 'dot bad';
    conn.textContent = 'NO LINK';
    conn.style.color = 'var(--rose)';
  }
}

// ════════════════════════════════════════════════════════════════════
//  VIEW SHELL HELPERS
// ════════════════════════════════════════════════════════════════════
function viewHead(tag, title, sub, ...actions) {
  return el('div', { class: 'view-head' },
    el('span', { class: 'view-tag' }, tag),
    el('span', { class: 'view-title' }, title),
    sub ? el('span', { class: 'view-sub' }, sub) : null,
    actions.length ? el('div', { class: 'view-actions' }, ...actions) : null,
  );
}
function panel(num, title, iconId, body, bodyPad) {
  return el('section', { class: 'panel fade-in' },
    el('div', { class: 'panel-head' },
      num ? el('span', { class: 'panel-num' }, num) : null,
      el('span', { class: 'panel-title' }, title),
      iconId ? icon(iconId, 'panel-ic') : null),
    el('div', { class: 'panel-body' + (bodyPad ? ' pb-pad' : '') }, body),
  );
}
// Translate a raw exception (or HTTP error message) into product-quality
// copy a financial reader can act on. Engineers can still get the trace
// via a collapsible disclosure. Same humane-error rule as the rest of
// the UI: never lead with "system failed", lead with what we know and
// what the user can do.
function friendlyErrorCopy(raw) {
  const s = String(raw || '').toLowerCase();
  if (s.includes('errno 35') || s.includes('resource temporarily') ||
      s.includes('readerror') || s.includes('connection reset')) {
    return {
      head: 'Source temporarily unreachable',
      msg: 'Doré couldn\'t finish reading from one of the upstream ' +
        'sources (an issuer page or an RPC endpoint timed out). Try ' +
        'RE-RUN in a moment — the background canary also retries every ' +
        'six hours, so the cached result will refresh on its own.',
    };
  }
  if (s.includes('429') || s.includes('rate limit') ||
      s.includes('quota')) {
    return {
      head: 'Search backend rate-limited',
      msg: 'The web-discovery backend (Brave + DuckDuckGo) hit a soft ' +
        'rate limit on this query. The cached result is still served ' +
        'instantly; a fresh search will retry on the next canary sweep.',
    };
  }
  if (s.includes('timeout') || s.includes('timed out')) {
    return {
      head: 'Source took too long',
      msg: 'An issuer page or RPC endpoint didn\'t respond within the ' +
        'deadline. This is usually transient; try again below, or wait ' +
        'for the next six-hourly background sweep.',
    };
  }
  if (s.includes('not configured') || s.includes('llmnotconfigured')) {
    return {
      head: 'LLM key not configured on this server',
      msg: 'The synthesis step needs an LLM_API_KEY. Set it in the ' +
        'server env and restart; the deterministic facts above are ' +
        'still valid.',
    };
  }
  if (s.includes('404') || s.includes('not found')) {
    return {
      head: 'Source URL has moved',
      msg: 'A document Doré expected to find returned 404 — the issuer ' +
        'has likely rotated their attestation URL. The background ' +
        'discovery sweep will try to find the new location on the next ' +
        'cycle; for an immediate refresh, use the button below.',
    };
  }
  // Generic fallback — NEVER expose the raw exception text to the user.
  // Whatever the underlying error was, the same recovery applies: try
  // again, or wait for the next background sweep. Engineers find the
  // full trace in the server logs and the Compendium event stream.
  return {
    head: 'Doré couldn\'t finish this run',
    msg: 'Something went wrong on the server. Try again below — most '
      + 'failures are transient. The background discovery thread retries '
      + 'every six hours regardless.',
  };
}

function errorBox(title, msg, trace, opts) {
  // Stack traces never appear in user-facing UI. Engineers find the
  // full picture in server logs + the Compendium event stream. The
  // friendly copy + retry button are the only surface here.
  // We do log the raw exception to the operations feed (terminal-style
  // logLine on F1) at a low level so an operator-mode user still has a
  // trail without seeing a wall of Python in the middle of the app.
  const friendly = friendlyErrorCopy(msg);
  if (msg) {
    logLine('ERR', 'TRACE', [
      seg(String(msg).split('\n')[0].slice(0, 96), 'd-warn'),
    ]);
  }
  const body = el('div', { class: 'error-box fade-in' },
    el('div', { class: 'eb-head' }, friendly.head),
    el('div', { class: 'eb-msg' }, friendly.msg));
  if (opts && typeof opts.onRetry === 'function') {
    const retry = el('button', {
      class: 'btn',
      style: 'margin-top:12px',
      onclick: opts.onRetry,
    }, icon('i-supply'),
       document.createTextNode(opts.retryLabel || 'TRY AGAIN'));
    body.append(retry);
  }
  return body;
}

// ════════════════════════════════════════════════════════════════════
//  MONITOR VIEW — the opening live stream
// ════════════════════════════════════════════════════════════════════
function viewMonitor() {
  app.innerHTML = '';
  _feedRendered = 0;
  app.append(viewHead('F1', 'MONITOR',
    'live operations feed · continuous on-chain surveillance'));

  const body = el('div', { class: 'monitor' });

  // feed zone — bar · labelled column header · scrolling feed
  const feed = el('div', { class: 'feed', id: 'op-feed' });
  body.append(el('div', { class: 'feed-zone' },
    el('div', { class: 'feed-bar' },
      el('span', { class: 'blink' }, '● LIVE'),
      el('span', {}, 'OPERATIONS LOG'),
      el('span', { class: 'feed-cols',
        title: 'Self-updating feed — every line is a real event. SUPPLY = '
          + 'real on-chain re-reads every 3.4s. Values are held at their '
          + 'last real read between polls — never simulated. Hover any '
          + 'column label below for what it means.' },
        'live · auto-streaming · hover a column for help')),
    feedHeaderRow(),
    feed));

  // grid zone
  const gridTable = el('table', { class: 'dtable' },
    el('thead', {}, el('tr', {},
      el('th', { class: 'th-mark' }, ''),
      el('th', {}, 'TOKEN'), el('th', {}, 'ISSUER'),
      el('th', { class: 'num' }, 'SUPPLY · NATIVE'),
      el('th', { class: 'num', title: tip.drift }, 'Δ DRIFT'),
      el('th', { class: 'num' }, 'CHAINS'),
      el('th', { class: 'num', title: tip.bridged }, 'BRIDGED %'),
      el('th', { class: 'num', title: tip.verifiedRatio }, 'VERIFIED'),
      el('th', {}, 'STATUS'),
      el('th', { title: 'Jump to this token’s F5 sanctions screen or F6 '
        + 'redemption assessment.' }, 'COMPLIANCE'))),
    el('tbody', { id: 'grid-rows' }));
  body.append(el('div', { class: 'grid-zone' },
    el('div', { class: 'grid-bar' },
      el('span', {}, 'INSTRUMENT MONITOR'),
      el('span', { class: 'feed-cols', style: 'margin-left:auto' },
        'live values · click a row → run analysis')),
    gridTable));

  app.append(body);

  // backfill the feed buffer + grid — newest-first, newest at the top.
  // a row left expanded keeps its inline detail panel directly beneath it.
  for (let i = STATE.feed.length - 1; i >= 0; i--) {
    const l = STATE.feed[i];
    feed.append(feedRowNode(l));
    if (l._open) feed.append(feedDetailPanel(l));
  }
  _feedRendered = STATE.feed.length;
  feed.scrollTop = 0;
  refreshGrid();
}

// ════════════════════════════════════════════════════════════════════
//  ANALYZE VIEW
// ════════════════════════════════════════════════════════════════════
let pollTimer = null;

// ── dynamic stage sub-narration ──────────────────────────────────────
// Each stage label gets 3-5 honest one-liners that cycle while the
// stage is active. Each line corresponds to an actual sub-step the
// backend performs — no invented chatter. Keyed by substrings of the
// stage label so server-side STAGES list changes don't break the
// mapping. Defaults to an empty array (no sub-narration) for unmatched
// stages.
function stageSubNarration(stages) {
  const tables = [
    { match: /supply|on-chain/i, lines: [
      'pinning the block number',
      'reading totalSupply via primary RPC',
      'cross-checking with the fallback endpoint',
      'tallying per-chain native and bridged supply',
      'comparing against last persisted reading',
    ] },
    { match: /attestation|locating|downloading/i, lines: [
      'checking the database for a curator override',
      'walking the issuer transparency page',
      'searching the web for a fresh report',
      'HEAD-checking each candidate PDF',
      'fetching the winning PDF',
    ] },
    { match: /extract|reserves from/i, lines: [
      'extracting plain text from the PDF',
      'identifying the as-of date',
      'parsing the reserves breakdown',
      'reconciling tokens outstanding',
      'scoring extraction confidence',
    ] },
    { match: /guardrail|check/i, lines: [
      'reserves positive · tokens positive',
      'breakdown sums match',
      'coverage ratio inside the plausible band',
      'attestation date valid',
      'every figure traces to a tool output',
    ] },
    { match: /sdn|sanctions|screen/i, lines: [
      'pulling the OFAC SDN list',
      'verifying SHA-256 of the SDN file',
      'screening every deployment address',
      'checking staleness against guardrail bounds',
    ] },
    { match: /classify|liquidity|tier/i, lines: [
      'walking the reserve breakdown',
      'classifying each line as liquid / moderate / illiquid',
      'computing liquid coverage',
      'computing net redemption flow',
    ] },
    { match: /corpus|reasoning frame/i, lines: [
      'retrieving included sources',
      'ranking passages by relevance',
      'pulling the top regulatory citations',
      'gathering recent web-search references',
    ] },
    { match: /synth|narrative|brief/i, lines: [
      'composing the deterministic facts block',
      'asking the LLM for a cited brief',
      'verifying every figure traces back',
      'rejecting any uncited claim',
      'finalising the editorial headline',
    ] },
  ];
  return (stages || []).map((label) => {
    for (const t of tables) {
      if (t.match.test(label)) return t.lines;
    }
    return [];
  });
}

// ── sidebar token-click target ───────────────────────────────────────
// When the user clicks a token in the sidebar (or the monitor grid),
// stay in whichever surface they're currently viewing rather than
// always bouncing to #analyze. On the sanctions or redemption surfaces
// the click runs that surface for the picked token in place; on any
// other surface (monitor, corpus, evals, analyst, compendium) default
// to analyze.
function currentSurfaceHash(symbol) {
  const view = (location.hash || '').replace(/^#/, '').split('/')[0];
  if (view === 'sanctions') return '#sanctions/' + symbol;
  if (view === 'redemptions') return '#redemptions/' + symbol;
  return '#analyze/' + symbol;
}

// ── background job tracker ──────────────────────────────────────────
// When the user navigates away from a token mid-run, the foreground
// poll exits but the server-side job keeps running. trackJob() picks
// it up at a low poll cadence (4s) and fires a toast when the result
// lands so the user can jump back to it instead of having to remember
// they ran it.
const TRACKED_JOBS = new Map();
const KIND_LABELS = {
  analyze: { surface: 'analysis', api: '/analyze/' },
  sanctions: { surface: 'sanctions screen', api: '/sanctions/' },
  redemptions: { surface: 'redemption assessment', api: '/redemption/' },
};

function trackJob(jobId, kind, symbol) {
  if (TRACKED_JOBS.has(jobId)) return;
  const cfg = KIND_LABELS[kind];
  if (!cfg) return;
  const meta = { kind, symbol, startedAt: Date.now(), timer: null };
  const stopTracking = () => {
    clearInterval(meta.timer);
    TRACKED_JOBS.delete(jobId);
  };
  const tick = async () => {
    // Quietly give up after 5min — the job has either died or the
    // server pool TTL ate it. Toast spam serves no one.
    if (Date.now() - meta.startedAt > 5 * 60_000) { stopTracking(); return; }
    let st;
    try { st = await api(cfg.api + jobId); }
    catch { return; }  // transient — try next tick
    if (st.status === 'running') return;
    stopTracking();
    if (st.status === 'done') {
      showJobToast('ok', kind, symbol, cfg.surface);
    } else if (st.status === 'error') {
      showJobToast('err', kind, symbol, cfg.surface);
    }
  };
  meta.timer = setInterval(tick, 4000);
  TRACKED_JOBS.set(jobId, meta);
  logLine('WATCH', kind.toUpperCase().slice(0, 8), [
    seg(symbol.padEnd(5), 'lg-sym'),
    seg('backgrounded', 'd-warn'),
    seg('toast on finish'),
  ]);
}

function showJobToast(kind, surfaceKind, symbol, surfaceLabel) {
  const stack = $('#toast-stack') || (() => {
    const s = document.createElement('div');
    s.id = 'toast-stack';
    document.body.append(s);
    return s;
  })();
  const ok = kind === 'ok';
  const toast = el('div', {
    class: 'toast ' + (ok ? 'toast-ok' : 'toast-err') + ' fade-in',
    onclick: () => {
      location.hash = '#' + surfaceKind + '/' + symbol;
      toast.remove();
    },
  },
    icon(ok ? 'i-ok' : 'i-error'),
    el('div', { class: 'toast-body' },
      el('div', { class: 'toast-head' },
        symbol + ' ' + surfaceLabel + (ok ? ' finished' : ' failed')),
      el('div', { class: 'toast-sub' },
        ok ? 'Click to view the result' : 'Click to retry')),
    el('button', {
      class: 'toast-x',
      title: 'dismiss',
      onclick: (e) => { e.stopPropagation(); toast.remove(); },
    }, '×'),
  );
  stack.append(toast);
  setTimeout(() => {
    toast.classList.add('toast-out');
    setTimeout(() => toast.remove(), 320);
  }, 12000);
}

// ── result freshness ─────────────────────────────────────────────────
// A compute is "fresh" for six hours — matches the server-side
// _CACHE_FRESH_S TTL in web/server.py. The background canary refreshes
// every artefact every 6h, so any cache miss after that window is
// genuinely a re-compute opportunity. Inside the window a revisit is
// served instantly from cache (no motions); past it the result is
// still shown instantly but the freshness strip prompts for refresh.
const FRESH_MS = 6 * 60 * 60 * 1000;

// human "computed 3m ago" from a UTC ISO timestamp (or null when unknown)
function freshnessAge(computedAt) {
  if (!computedAt) return null;
  const t = Date.parse(String(computedAt).replace(' ', 'T'));
  if (Number.isNaN(t)) return null;
  return Date.now() - t;
}
function fmtAgo(ms) {
  if (ms == null) return 'just now';
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.round(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.round(h / 24) + 'd ago';
}

// ── per-(surface,symbol) latency memory ─────────────────────────────
// We keep the last few real run durations in localStorage so the user
// can be told what to expect before the next run completes. The
// average drives the "~Xs typical" hint beside the live clock, the
// expected-vs-actual styling on the scanbar, and a "last run: Ns"
// tooltip on the RE-RUN button. Fall-back defaults cover first-time
// visitors who have no samples yet — typical real-world figures from
// production runs, deliberately conservative so the user is pleasantly
// surprised when a run is faster.
const _LAT_KEY = (sf, sym) => 'rs.lat.' + sf + '.' + sym;
const _LAT_MAX = 8;
const _LAT_DEFAULT = { attestation: 28, sanctions: 14, redemption: 26, evals: 65 };

function recordLatency(surface, symbol, seconds) {
  if (!surface || !symbol || !seconds || seconds < 0.5) return;
  try {
    const arr = JSON.parse(localStorage.getItem(_LAT_KEY(surface, symbol)) || '[]');
    arr.push(Math.round(seconds * 10) / 10);
    while (arr.length > _LAT_MAX) arr.shift();
    localStorage.setItem(_LAT_KEY(surface, symbol), JSON.stringify(arr));
  } catch { /* private mode / quota — silent */ }
}

function expectedLatency(surface, symbol) {
  try {
    const arr = JSON.parse(localStorage.getItem(_LAT_KEY(surface, symbol)) || '[]');
    if (arr.length) {
      const sum = arr.reduce((a, b) => a + b, 0);
      return { seconds: Math.round(sum / arr.length), source: 'measured', n: arr.length };
    }
  } catch { /* fall through */ }
  return { seconds: _LAT_DEFAULT[surface] || 25, source: 'default', n: 0 };
}

// Plain-English wait hint shown beside the live clock + on the
// RE-RUN tooltip. "~22s typical (last 5 runs)" or "~25s expected
// (first run)" — never just a bare number with no context.
function latencyHint(surface, symbol) {
  const e = expectedLatency(surface, symbol);
  if (e.source === 'measured') {
    return '~' + e.seconds + 's typical (last ' + e.n + ' run' +
      (e.n === 1 ? '' : 's') + ')';
  }
  return '~' + e.seconds + 's expected (first run for ' + symbol + ')';
}

// the quiet "computed Nm ago" line shown on every rendered result. Once
// the result is older than ten minutes it grows a prominent REFRESH
// element; clicking it re-runs with refresh:true (a real recompute, so
// the staged motions legitimately play).
function freshnessStrip(computedAt, onRefresh) {
  const ms = freshnessAge(computedAt);
  const stale = ms != null && ms > FRESH_MS;
  const strip = el('div', {
    class: 'fresh-strip fade-in' + (stale ? ' stale' : ''),
  });
  strip.append(el('span', { class: 'fresh-dot' }));
  strip.append(el('span', { class: 'fresh-txt' },
    'computed ' + fmtAgo(ms)));
  if (stale) {
    strip.append(el('span', { class: 'fresh-flag' },
      'result is over six hours old'));
  }
  // One canonical refresh action, contextual to result age: prominent
  // when stale, soft-ghost when fresh. Tooltip includes the
  // last-N-runs average so the user knows what to expect before they
  // commit to waiting through a recompute. Surface + symbol are
  // resolved from the route so this helper stays generic across
  // F2/F4/F5/F6.
  const route = (location.hash || '').replace(/^#/, '').split('/')[0];
  const surface = ({
    analyze: 'attestation', sanctions: 'sanctions',
    redemptions: 'redemption', evals: 'evals',
  })[route] || 'attestation';
  const sym = surface === 'evals' ? 'suite' : STATE.activeSymbol;
  const lat = sym ? expectedLatency(surface, sym) : null;
  const latLine = lat
    ? ' A recompute typically takes about ' + lat.seconds + ' seconds'
      + (lat.source === 'measured'
          ? ' (averaged over your last ' + lat.n + ' run'
            + (lat.n === 1 ? '' : 's') + ').'
          : '.')
    : '';
  const btn = el('button', {
    class: 'btn ' + (stale ? 'fresh-refresh' : 'ghost'),
    title: stale
      ? 'Re-run a full recompute — live RPCs + LLM. The staged progress '
        + 'plays because this is a genuine recompute.' + latLine
      : 'Re-run from scratch. The cached result is still served instantly '
        + 'if you re-open this page.' + latLine,
    onclick: onRefresh,
  }, icon('i-supply'), stale ? 'REFRESH' : 'RE-RUN');
  strip.append(btn);
  return strip;
}

function viewAnalyze(symbolFromHash) {
  app.innerHTML = '';
  _feedRendered = STATE.feed.length;

  const input = el('input', {
    class: 'tinput', type: 'text', placeholder: 'SYMBOL',
    autocomplete: 'off', spellcheck: 'false',
    value: symbolFromHash || STATE.activeSymbol || '',
  });
  const runBtn = el('button', { class: 'btn' }, icon('i-agent'), 'RUN');
  // One canonical refresh action: the freshnessStrip beneath every result
  // carries a contextual REFRESH that re-runs with refresh=true. Removing
  // the duplicate "FORCE REFRESH" header button keeps the surface clean.
  app.append(viewHead('F2', 'ANALYZE',
    'reserve attestation reconciled against live on-chain supply',
    input, runBtn));

  const mount = el('div', { class: 'view-body', id: 'an-mount' });
  app.append(mount);

  const run = () => {
    const sym = input.value.trim().toUpperCase();
    if (!sym) { flagEmptyInput(input); return; }
    // Always trigger a run. Setting the hash only fires `hashchange` when
    // the value actually changes, so when it is already the target we run
    // the render path directly instead of relying on the event.
    const target = '#analyze/' + sym;
    if (location.hash === target) startAnalysis(sym, mount);
    else location.hash = target;
  };
  runBtn.addEventListener('click', run);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });

  if (symbolFromHash) {
    // Snappy on-load: try the Store-backed cache FIRST. If a result
    // exists at any age, render it instantly without the job round-trip.
    // For stale results we still render immediately; the freshness strip
    // tells the user it's old and offers REFRESH. Only a true cache miss
    // (or an explicit refresh later) triggers the staged job flow.
    renderCachedOrRun('attestation', symbolFromHash, mount,
                      () => startAnalysis(symbolFromHash, mount, true),
                      (sym, m) => startAnalysis(sym, m));
  } else {
    mount.append(el('div', { class: 'empty' },
      icon('i-agent'),
      el('b', {}, 'Select an instrument'),
      el('div', {}, 'Pick a token from the sidebar, or type a symbol and RUN. ' +
        'The agent reads live supply, extracts the latest attestation, runs ' +
        'guardrails, and synthesises a cited analysis.')));
    // signed in → show the saved analysis history; anonymous → an
    // invitation to sign in so future runs are kept.
    renderHistory(mount);
  }
}

// Generic cached-first loader for analyze / sanctions / redemption.
// surface is the server-side key ("attestation"/"sanctions"/"redemption");
// onRefresh is what RUNs when the user hits REFRESH on a stale result;
// onMiss is what RUNs on a true cache miss (no row at all yet).
async function renderCachedOrRun(surface, symbol, mount, onRefresh, onMiss) {
  STATE.activeSymbol = symbol;
  setAnalyzeHeaderMark(symbol);
  refreshInstruments();
  // Briefly show a quiet placeholder so the surface never flashes empty
  // even if the cache lookup itself takes a beat.
  mount.innerHTML = '';
  const placeholder = el('div', { class: 'empty' },
    icon('i-agent'),
    el('b', {}, 'Loading ' + symbol + '…'));
  mount.append(placeholder);
  let payload;
  try {
    payload = await api('/cached/' + surface + '/' + symbol);
  } catch (e) {
    mount.removeChild(placeholder);
    onMiss(symbol, mount);
    return;
  }
  if (!payload || !payload.cached) {
    mount.removeChild(placeholder);
    onMiss(symbol, mount);
    return;
  }
  mount.removeChild(placeholder);
  const result = payload.result;
  const renderer = ({
    attestation: renderAnalysis,
    sanctions: renderSanctions,
    redemption: renderRedemption,
  })[surface];
  if (!renderer) {
    onMiss(symbol, mount);
    return;
  }
  // Cached hit: render instantly. The freshness strip carries the
  // computed_at + REFRESH; for stale rows it pulses prominently.
  renderer(result, null, mount, payload.computed_at, onRefresh);
  logLine('OK', surface.toUpperCase().slice(0, 8), [
    seg(symbol.padEnd(5), 'lg-sym'),
    seg(payload.stale ? 'stale cache' : 'fresh cache', payload.stale ? 'd-warn' : 'd-up'),
  ]);
}

// ── analysis history — the saved-runs ledger, signed-in users only ──
async function renderHistory(mount) {
  if (AUTH.client && !AUTH.signedIn()) {
    mount.append(savePrompt('keep a history of every analysis you run'));
    return;
  }
  if (!AUTH.signedIn()) return;  // no auth backend — nothing to show
  let data;
  try { data = await api('/history'); }
  catch { return; }  // history is a bonus surface — never break the view
  const list = el('div', { class: 'hist-list' });
  if (!data.analyses.length) {
    list.append(el('div', { class: 'hist-empty' },
      'No saved analyses yet — run one and it will persist to your account.'));
  } else {
    data.analyses.forEach((a) => {
      const when = a.created_at
        ? new Date(a.created_at).toLocaleString() : '';
      const row = el('div', { class: 'hist-row' },
        el('span', { class: 'h-sym' }, a.symbol || '—'),
        el('span', { class: 'h-surface' }, a.surface || 'attestation'),
        el('span', { class: 'h-status ' + (a.status || '') }, a.status || ''),
        el('span', { class: 'h-time' }, when));
      if (a.symbol) {
        row.addEventListener('click',
          () => { location.hash = '#analyze/' + a.symbol; });
      }
      list.append(row);
    });
  }
  mount.append(panel(null,
    'YOUR ANALYSIS HISTORY · ' + (data.count || 0), 'i-doc', list));
}

// place the instrument's brand mark in the ANALYZE view header
function setAnalyzeHeaderMark(symbol) {
  const head = $('.view-head');
  if (!head) return;
  const existing = $('.view-mark', head);
  if (existing) existing.remove();
  if (!symbol) return;
  const tag = $('.view-tag', head);
  head.insertBefore(tokenMark(symbol, 'view-mark'), tag.nextSibling);
}

async function startAnalysis(symbol, mount, refresh = false) {
  if (pollTimer) clearInterval(pollTimer);
  STATE.activeSymbol = symbol;
  setAnalyzeHeaderMark(symbol);
  refreshInstruments();
  mount.innerHTML = '';

  logLine('WORK', 'ANALYZE', [
    seg(symbol.padEnd(5), 'lg-sym'),
    seg('job queued', 'lg-val'),
    seg(refresh ? 'mode=refresh' : 'mode=cached',
      refresh ? 'd-warn' : 'd-up'),
  ]);

  let job;
  try {
    const tier = getTier();
    if (tier === 'deep') startDeepCountdown(Date.now() + 120000);
    job = await api('/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, refresh, tier }),
    });
    if (job.status === 'done') stopDeepCountdown();
  } catch (e) {
    logLine('ERR', 'ANALYZE', [
      seg(symbol.padEnd(5), 'lg-sym'), seg('START-FAIL', 'd-warn'),
      seg(String(e.message).slice(0, 48)),
    ]);
    mount.append(errorBox('Could not start analysis · ' + symbol,
      e.message, undefined,
      { onRetry: () => startAnalysis(symbol, mount, true),
        retryLabel: 'TRY AGAIN' }));
    return;
  }

  // Cached hit — the result was already computed and is still fresh. Render
  // it instantly, no job to poll and no staged motions.
  if (job.status === 'done') {
    const m = job.result.metrics;
    const gaps = (job.result.gaps || []).length;
    logLine('OK', 'ANALYZE', [
      seg(symbol.padEnd(5), 'lg-sym'),
      seg('cached', 'd-up'),
      seg('att' + (m ? fmtPct(m.attested_coverage) : 'n/a'),
        m ? covClass(m.attested_coverage) : ''),
      seg('lv' + (m ? fmtPct(m.live_coverage) : 'n/a'),
        m ? covClass(m.live_coverage) : ''),
      seg('gap' + gaps, gaps ? 'd-warn' : 'd-up'),
    ]);
    mount.innerHTML = '';
    renderAnalysis(job.result, null, mount, job.computed_at,
      () => startAnalysis(symbol, mount, true));
    return;
  }

  const stages = job.stages || [];
  const t0 = Date.now();
  const clockSpan = el('span', { class: 'rs-tk' }, '0s');
  // Each stage carries one parent label + a wrap for the dynamic
  // sub-narration that cycles while that stage is active.
  const stageNodes = stages.map((label, i) =>
    el('div', { class: 'rstage', 'data-i': i },
      el('div', { class: 'rs-mark' }),
      el('div', { class: 'rs-text' },
        el('div', { class: 'rs-label' }, label),
        el('div', { class: 'rs-sub' }, '')),
      el('span', { class: 'rs-tk' }, 'S' + pad2(i + 1))));

  // Expected-wait hint: drives the live clock styling + the friendly
  // "About 22 seconds typically" line beneath the stages so the user
  // knows roughly how long this should take.
  const expected = expectedLatency('attestation', symbol);
  const waitHint = el('div', { class: 'rs-wait' },
    el('span', { class: 'rs-wait-mark' }, '◇'),
    el('span', { class: 'rs-wait-txt' }, latencyHint('attestation', symbol)));
  const scanbar = el('div', { class: 'scanbar' });
  const progress = panel(null, 'JOB ' + job.job_id.toUpperCase() + ' · ' + symbol, 'i-agent',
    el('div', {},
      el('div', { class: 'run-stages' }, ...stageNodes),
      scanbar,
      waitHint));
  // clock into the panel head
  $('.panel-head', progress).insertBefore(
    el('span', { class: 'rs-tk', style: 'margin-left:auto' }, ''), $('.panel-ic', progress));
  const clockHost = $('.panel-head .rs-tk', progress);
  mount.append(progress);

  // Honest sub-narration for each stage — what actually happens in
  // that phase, surfaced as a rotating one-liner. Not invented chatter;
  // each line corresponds to a real step the backend takes.
  const SUB_NARRATION = stageSubNarration(stages);

  let stage = 0;
  let subIdx = 0;
  let subTimer = null;
  const cycleSub = () => {
    const lines = SUB_NARRATION[stage] || [];
    if (!lines.length) return;
    const node = stageNodes[stage] && stageNodes[stage].querySelector('.rs-sub');
    if (!node) return;
    node.textContent = lines[subIdx % lines.length];
    node.classList.remove('rs-sub-tick');
    void node.offsetWidth;  // restart the CSS fade
    node.classList.add('rs-sub-tick');
    subIdx++;
  };
  const advance = () => {
    stageNodes.forEach((n, i) => {
      n.className = 'rstage' + (i < stage ? ' done' : i === stage ? ' active' : '');
      // Clear sub-narration on completed and pending stages.
      const sub = n.querySelector('.rs-sub');
      if (sub && i !== stage) sub.textContent = '';
    });
    subIdx = 0;
    cycleSub();
  };
  advance();
  if (subTimer) clearInterval(subTimer);
  subTimer = setInterval(cycleSub, 1700);
  let lastLogged = -1;
  const stageTimer = setInterval(() => {
    if (stage < stages.length - 1) { stage++; advance(); }
  }, 5200);
  const clockTimer = setInterval(() => {
    const elapsedS = Math.round((Date.now() - t0) / 1000);
    // "8s / ~22s" while we're inside the expected window; once we've
    // overshot, drop the divisor and flip the colour so the user knows
    // this run is unusually slow rather than wondering if it stalled.
    const over = elapsedS > expected.seconds * 1.5;
    const wayOver = elapsedS > expected.seconds * 2.5;
    clockHost.textContent = over
      ? elapsedS + 's'
      : elapsedS + 's / ~' + expected.seconds + 's';
    clockHost.className = 'rs-tk' + (wayOver ? ' rs-tk-slow' :
      over ? ' rs-tk-overrun' : '');
    // Switch the friendly wait line to a reassuring "still working"
    // message once we're materially past the typical time, rather than
    // leaving the original "~Ns typical" up when it's clearly wrong.
    if (wayOver) {
      waitHint.classList.add('rs-wait-slow');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'Taking longer than usual. The issuer page or LLM is slow today; '
        + 'still working.';
    } else if (over) {
      waitHint.classList.add('rs-wait-over');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'A little past the typical ' + expected.seconds + 's, still on track.';
    }
    // Scanbar fill mirrors the elapsed/expected ratio so the user sees
    // material progress; clamps at 96% so it never falsely shows "done".
    const pct = Math.min(96, Math.round((elapsedS / expected.seconds) * 100));
    scanbar.style.setProperty('--scan-pct', pct + '%');
    if (stage !== lastLogged) {
      lastLogged = stage;
      logLine('WORK', 'ANALYZE', [
        seg(symbol.padEnd(5), 'lg-sym'),
        seg('stage ' + (stage + 1) + '/' + stages.length, 'lg-val'),
        seg((elapsedS + 's').padStart(4)),
        seg(stages[stage]),
      ]);
    }
  }, 250);

  const finish = () => {
    clearInterval(stageTimer); clearInterval(clockTimer);
    clearInterval(pollTimer); pollTimer = null;
    if (subTimer) { clearInterval(subTimer); subTimer = null; }
  };

  pollTimer = setInterval(async () => {
    if (STATE.activeSymbol !== symbol) {
      // User moved on. Hand the job off to the background tracker so
      // a toast lands when it eventually finishes.
      trackJob(job.job_id, 'analyze', symbol);
      finish();
      return;
    }
    let st;
    try { st = await api('/analyze/' + job.job_id); }
    catch (e) {
      finish(); mount.innerHTML = '';
      logLine('ERR', 'ANALYZE', [
        seg(symbol.padEnd(5), 'lg-sym'), seg('POLL-FAIL', 'd-warn'),
        seg(String(e.message).slice(0, 48)),
      ]);
      mount.append(errorBox('Polling failed · ' + symbol, e.message,
        undefined,
        { onRetry: () => startAnalysis(symbol, mount, true),
          retryLabel: 'TRY AGAIN' }));
      return;
    }
    if (st.status === 'running') return;
    finish();
    stage = stages.length; advance();
    mount.innerHTML = '';
    stopDeepCountdown();
    if (st.status === 'error') {
      logLine('ERR', 'ANALYZE', [
        seg(symbol.padEnd(5), 'lg-sym'), seg('FAILED', 'd-warn'),
        seg(String(st.error).slice(0, 56)),
      ]);
      mount.append(errorBox('Analysis failed · ' + symbol, st.error, st.trace,
        { onRetry: () => startAnalysis(symbol, mount, true),
          retryLabel: 'TRY AGAIN' }));
    } else {
      const m = st.result.metrics;
      const gaps = (st.result.gaps || []).length;
      // Record so future runs of this token get a tighter "~Ns typical" hint.
      recordLatency('attestation', symbol, st.elapsed);
      logLine('OK', 'ANALYZE', [
        seg(symbol.padEnd(5), 'lg-sym'),
        seg('done ' + st.elapsed + 's', 'lg-val'),
        seg('att' + (m ? fmtPct(m.attested_coverage) : 'n/a'),
          m ? covClass(m.attested_coverage) : ''),
        seg('lv' + (m ? fmtPct(m.live_coverage) : 'n/a'),
          m ? covClass(m.live_coverage) : ''),
        seg('gap' + gaps, gaps ? 'd-warn' : 'd-up'),
      ]);
      renderAnalysis(st.result, st.elapsed, mount,
        new Date().toISOString(),
        () => startAnalysis(symbol, mount, true));
      // persistence is gated on an account: signed-in runs are saved to
      // history, anonymous runs are transient. Reflect that, invitingly.
      if (AUTH.client && !AUTH.signedIn()) {
        mount.append(savePrompt(
          'save this analysis to your history and revisit it later'));
      } else if (AUTH.signedIn()) {
        logLine('OK', 'ANALYZE', [seg(symbol.padEnd(5), 'lg-sym'),
          seg('saved to history', 'd-up')]);
      }
    }
  }, 1400);
}

// ── render a completed Analysis ──────────────────────────────────────
function renderAnalysis(a, elapsed, mount, computedAt, onRefresh) {
  const m = a.metrics;
  const att = a.attestation;
  const supply = a.supply || {};

  // freshness — a quiet "computed Nm ago" line; once stale (>10min) it
  // surfaces a prominent REFRESH that triggers a real recompute.
  if (computedAt !== undefined && onRefresh) {
    mount.append(freshnessStrip(computedAt, onRefresh));
  }

  // ── DORÉ BRIEF — editorial top-of-view synthesis (when available) ──
  // Sits above everything else: headline, key points, relevant news.
  // Distinct gold-bordered hero panel. Omitted entirely if the brief
  // couldn't be generated (LLM down / quota exhausted) — never renders
  // a placeholder.
  if (a.brief && a.brief.headline) {
    mount.append(aiBriefHero(a.brief));
  }

  // bridged_share is a @property — not in dataclasses.asdict(). Compute it.
  const nativeS = Number(supply.native_supply || 0);
  const bridgedS = Number(supply.bridged_supply || 0);
  const gross = nativeS + bridgedS;
  const bridgedShare = gross ? bridgedS / gross : null;

  // Summary strip — the "as of" cell adapts to the backing model so it
  // shows the on-chain read date for crypto / synthetic / algorithmic
  // tokens rather than a useless dash.
  const isFiatA = (a.backing_model || 'fiat_reserves') === 'fiat_reserves';
  const asOfLabelA = isFiatA ? 'ATTESTED AS OF' : 'ON-CHAIN AS OF';
  const asOfValueA = att ? att.as_of_date
    : (supply.read_at ? supply.read_at.slice(0, 10) : '—');
  mount.append(el('div', { class: 'strip fade-in' },
    stripCell(a.symbol, 'TOKEN', 'v-gold'),
    stripCell(asOfValueA, asOfLabelA),
    stripCell(fmtUSD(supply.total_supply), 'HEADLINE · NATIVE'),
    stripCell(elapsed != null ? elapsed + 's' : '—', 'RUN TIME'),
  ));

  // ── 01 SNAPSHOT ──
  // when coverage is n/a, the cell copy reads from BACKING MODEL: a
  // crypto-collateralised token (DAI/USDe/GHO) never has a fiat
  // attestation by design — the dead-end "no attestation could be
  // resolved" line is wrong copy for them. Only fiat-backed tokens that
  // FAILED to fetch get the "see GAPS" framing.
  const naCov = !m;
  const naCovDesc = naCoverageDesc(a);  // backing-model aware
  const cov = el('div', { class: 'cov-row' },
    el('div', { class: 'cov-cell', title: naCov ? tip.na.coverage : tip.attested },
      el('div', { class: 'cov-kick' }, naCov
        ? coverageKickerForNa(a, 'ATTESTED COVERAGE')
        : 'ATTESTED COVERAGE — HONEST BACKING'),
      el('div', { class: 'cov-big ' + covClass(m && m.attested_coverage) },
        m ? fmtPct(m.attested_coverage) : naCoverageBig(a)),
      el('div', { class: 'cov-desc' }, naCov
        ? naCovDesc
        : 'Reserves ÷ attested tokens outstanding. The issuer’s stated backing ' +
          'ratio at the attestation date — unaffected by later supply moves.')),
    el('div', { class: 'cov-cell', title: naCov ? tip.na.coverage : tip.live },
      el('div', { class: 'cov-kick' }, naCov
        ? coverageKickerForNa(a, 'LIVE COVERAGE')
        : 'LIVE COVERAGE — DRIFT-AFFECTED'),
      el('div', { class: 'cov-big ' + covClass(m && m.live_coverage) },
        m ? fmtPct(m.live_coverage) : naCoverageBig(a)),
      el('div', { class: 'cov-desc' }, naCov
        ? naCovDesc
        : 'Attested reserves ÷ current on-chain supply. Diverges from the attested ' +
          'ratio as supply changes after the attestation date.')),
  );

  const mgrid = el('div', { class: 'mgrid' },
    mcell('ON-CHAIN SUPPLY · NATIVE', fmtNum(supply.total_supply, 0), 'v-paper',
      `${(supply.per_chain || []).length} deployment(s) · excludes bridged`, 'i-supply'),
    mcell('ATTESTED RESERVES', att ? fmtUSD(att.total_reserves) : '—', 'v-gold',
      att ? 'from the latest attestation'
          : naFieldNote('see AI Context above'),
      'i-doc', att ? null : tip.na.reserves),
    mcell('TOKENS OUTSTANDING', att ? fmtNum(att.tokens_outstanding, 0) : '—',
      'v-paper',
      att ? 'per the attestation' : naFieldNote('see AI Context above'),
      'i-metric', att ? null : tip.na.tokens),
    mcell('EXTRACTION CONFIDENCE', att ? fmtPct(att.confidence) : '—',
      att && att.confidence >= 0.6 ? 'v-green' : 'v-amber',
      att ? 'LLM structured-extraction score'
          : naFieldNote('populates when a PDF resolves'),
      'i-cite', att ? tip.confidence : tip.na.confidence),
    mcell('STALENESS', m && att ? m.staleness_days + ' days' : '—',
      m && att && m.staleness_days > 35 ? 'v-amber' : 'v-paper',
      m && att ? 'age of the attestation'
               : naFieldNote('populates when a PDF resolves'),
      'i-gate', m && att ? tip.staleness : tip.na.staleness),
    mcell('SUPPLY DRIFT', m && m.supply_drift != null ? fmtPct(m.supply_drift) : '—',
      'v-paper',
      m && m.supply_drift != null ? 'supply move since attestation'
                                  : naFieldNote('needs an attestation baseline'),
      'i-metric', m && m.supply_drift != null ? tip.drift : tip.na.drift),
  );

  const snapBody = el('div', {});

  // Backing model badge + lineage banner — sit above everything so the
  // reader knows the lineage before looking at any number.
  snapBody.append(backingModelStrip(a));
  snapBody.append(dataLineageBanner(supply, m));

  // ── When the deterministic pipeline could not resolve an attestation,
  // the LLM augmentation card becomes the PRIMARY content. The bare
  // n/a fields move into a collapsed disclosure. Same pattern as the
  // redemption surface — never let bare n/a sit front and centre when
  // we have ANY contextual answer to give.
  const augCards = a.augmentations || [];
  if (!att && augCards.length > 0) {
    augCards.forEach((ctx) => snapBody.append(augmentationCard(ctx)));
    // On-chain supply IS verified — show it on its own row.
    snapBody.append(el('div', { class: 'mgrid' },
      mcell('ON-CHAIN SUPPLY · NATIVE', fmtNum(supply.total_supply, 0),
        'v-paper',
        `${(supply.per_chain || []).length} deployment(s) · excludes bridged`,
        'i-supply'),
    ));
    // Detailed reserve fields under a quiet disclosure. The summary
    // language is neutral (not "no attestation available") because the
    // AI Context above already serves the answer; this just exposes the
    // empty deterministic fields for trace-readers. A small footnote
    // names why the fetcher couldn't extract the PDF this run.
    const naDetails = el('details', { class: 'na-disclosure' });
    naDetails.append(el('summary', {},
      el('span', { class: 'na-disc-glyph' }, '⊕'),
      el('b', {}, 'Reserve detail fields '),
      el('span', { class: 'na-disc-hint' },
        '(coverage, reserve composition, staleness; populated when a '
        + 'fresh attestation PDF resolves)')));
    const inner = el('div', { class: 'na-disc-body' });
    inner.append(cov);
    inner.append(mgrid);
    // Tiny operator note: WHY the automated extractor came up empty
    // this run. Quiet, factual, no "could not"/"failed" framing.
    inner.append(el('div', { class: 'na-disc-footnote' },
      el('span', { class: 'glyph' }, '§'),
      'Automated fetch returned no PDF this run — most often the issuer '
      + 'page is JavaScript-rendered, the attestation URL rotated, or the '
      + 'document is gated behind a SPA. The AI Context block above '
      + 'covers backing structure; the cached attestation URL (if any) '
      + 'remains in the Compendium.'));
    naDetails.append(inner);
    snapBody.append(naDetails);
  } else {
    // Normal path: attestation resolved (or it's a crypto/synthetic/algo
    // token where the cov row already shows the by-design ∞/—).
    snapBody.append(cov);
    snapBody.append(mgrid);
    augCards.forEach((ctx) => snapBody.append(augmentationCard(ctx)));
  }

  // supply provenance
  snapBody.append(el('div', { class: 'sub-head' }, 'SUPPLY PROVENANCE — NATIVE VS BRIDGED'));
  snapBody.append(el('div', { class: 'mgrid', style: 'border-top:none' },
    mcell('NATIVE SUPPLY', fmtUSD(nativeS), 'v-green',
      'genuine issuance — forms the headline', 'i-supply'),
    mcell('BRIDGED SUPPLY', fmtUSD(bridgedS),
      bridgedS > 0 ? 'v-amber' : 'v-muted',
      'wrapped / bridged copies — shown, excluded', 'i-chain'),
    mcell('BRIDGED SHARE', fmtPct(bridgedShare),
      bridgedShare ? 'v-amber' : 'v-muted',
      'bridged ÷ (native + bridged)', 'i-metric', tip.bridged),
  ));
  snapBody.append(el('div', { class: 'prov-note' },
    icon('i-info'),
    el('div', {},
      el('b', {}, 'Headline supply is native-only. '),
      'Bridged copies are collateralised by locked native supply — summing both ' +
      'would double-count, so bridged is surfaced as its own metric but excluded ' +
      'from the headline figure.')));
  if (supply.read_at) {
    snapBody.append(el('div', { class: 'prov-stamp' },
      el('span', { class: 'glyph' }, '§'),
      'on-chain reads taken at ' + supply.read_at));
  }

  // per-chain table
  if ((supply.per_chain || []).length) {
    snapBody.append(el('div', { class: 'sub-head' }, 'PER-CHAIN SUPPLY BREAKDOWN'));
    const rows = supply.per_chain.map((c) => {
      const isBridged = c.kind === 'bridged';
      const verified = c.verified !== false;
      const expUrl = explorerUrl(c.chain, c.contract);
      const addrCell = expUrl
        ? el('a', {
            class: 'addr addr-link',
            href: expUrl, target: '_blank', rel: 'noopener noreferrer',
            title: 'open contract on ' + c.chain + ' explorer' },
            el('span', {}, c.contract),
            el('span', { class: 'addr-ext' }, '↗'))
        : el('span', { class: 'addr' }, c.contract);
      return el('tr', {},
        el('td', {}, el('span', { class: 'chain-id' },
          chainMark(c.chain, 'cmark-tbl'), c.chain)),
        el('td', {}, addrCell),
        el('td', {}, el('span', {
          class: 'kind has-tip kind-' + (isBridged ? 'bridged' : 'native'),
          title: isBridged ? tip.kindBridged : tip.kindNative },
          isBridged ? 'bridged' : 'native')),
        el('td', {}, el('span', {
          class: 'vmark has-tip ' + (verified ? 'vmark-ok' : 'vmark-no'),
          title: verified ? tip.verified : tip.unverified },
          icon(verified ? 'i-ok' : 'i-warn'),
          verified ? 'verified' : 'unverified')),
        el('td', { class: 'num dim' }, c.decimals),
        el('td', { class: 'num ' + (isBridged ? 'v-amber' : 'v-paper') }, fmtNum(c.supply, 0)),
        el('td', {}, consensusBadge(c.consensus || '')),
      );
    });
    snapBody.append(el('table', { class: 'dtable' },
      el('thead', {}, el('tr', {},
        el('th', {}, 'CHAIN'), el('th', {}, 'CONTRACT'),
        el('th', { title: tip.kindNative + '  /  ' + tip.kindBridged }, 'KIND'),
        el('th', { title: tip.verified }, 'VERIFIED'),
        el('th', { class: 'num' }, 'DECIMALS'),
        el('th', { class: 'num' }, 'SUPPLY'),
        el('th', { title: 'How many RPC endpoints corroborated this read. '
          + '2/2 agree = highest confidence; 1/2 single source = fallback '
          + 'unreachable so the value is uncorroborated; DISAGREEMENT = '
          + 'endpoints returned different values — investigate before trusting.' },
          'CONSENSUS'))),
      el('tbody', {}, ...rows)));

    // Partial-result banner — render LOUD if any chain failed entirely.
    if (supply.complete === false) {
      snapBody.append(el('div', { class: 'gap gap-critical' },
        icon('i-warn'),
        el('div', { class: 'gap-msg' },
          el('b', {}, 'PARTIAL TOTAL '),
          `— ${supply.chains_read || 0} of ${supply.chains_expected || 0} chains read. `,
          `Failed: ${(supply.failed_chains || []).join(', ') || '?'}. `,
          'The headline figure understates true circulation. '
          + 'Do not treat as authoritative.')));
    }
  }
  if ((supply.warnings || []).length) {
    snapBody.append(el('div', { class: 'sub-head' }, 'SUPPLY WARNINGS'));
    supply.warnings.forEach((w) => snapBody.append(
      el('div', { class: 'gap gap-warn' }, icon('i-warn'), el('div', { class: 'gap-msg' }, w))));
  }
  // Augmentation block — LLM-generated context filling gaps the
  // deterministic pipeline couldn't. Visually distinct (AI CONTEXT
  // badge) and never confused with verified figures.
  (a.augmentations || []).forEach((ctx) => snapBody.append(augmentationCard(ctx)));

  mount.append(panel('01', 'SNAPSHOT', 'i-facts', snapBody));

  // ── 02 GUARDRAIL CHECKS ──
  const checks = a.checks || [];
  mount.append(panel('02', `GUARDRAIL CHECKS · ${checks.length}`, 'i-gate',
    checks.length
      ? el('div', {}, ...checks.map(renderCheck))
      : el('div', { class: 'empty' }, icon('i-gate'), el('b', {}, 'No checks ran.'))));

  // ── 03 REASONING FRAME ──
  const passages = rankPassages(a.passages);
  mount.append(panel('03', `REASONING FRAME · CORPUS · ${passages.length}`, 'i-frame',
    passages.length
      ? el('div', { class: 'passage-list' },
          ...passages.map((p, i) => renderPassage(p, i + 1)))
      : el('div', { class: 'empty' }, icon('i-frame'),
          el('b', {}, 'No corpus passages retrieved'),
          el('div', {}, 'Judgements remain unsupported until source text is staged and ingested.'))));

  // ── 04 ANALYSIS NARRATIVE ──
  mount.append(panel('04', 'ANALYSIS', 'i-agent',
    a.narrative
      ? el('div', { class: 'narrative', html: markdown(a.narrative) })
      : el('div', { class: 'empty' }, icon('i-agent'), el('b', {}, 'No narrative synthesised.'))));

  // ── 05 CONFIDENCE & GAPS ──
  const gaps = a.gaps || [];
  const confBody = el('div', {});
  if (att) {
    confBody.append(el('div', { class: 'pb-pad', style: 'padding-bottom:4px' },
      el('div', { class: 'cov-kick' }, 'ATTESTATION EXTRACTION CONFIDENCE'),
      el('div', { style: 'display:flex;align-items:center;gap:12px;margin-top:6px' },
        el('div', { class: 'mcell-val ' + (att.confidence >= 0.6 ? 'v-green' : 'v-amber'),
          style: 'margin:0' }, fmtPct(att.confidence)),
        el('div', { class: 'cbar', style: 'flex:1' },
          el('div', { class: 'cfill', style: `width:${Math.min(100, att.confidence * 100)}%` }))),
      att.source_url
        ? el('div', { class: 'cite-row' },
            citeChip('source attestation', att.source_url, att.source_pages))
        : null));
  }
  confBody.append(el('div', { class: 'sub-head' }, `GAPS & OPEN ITEMS · ${gaps.length}`));
  if (gaps.length) {
    gaps.forEach((g) => confBody.append(renderGap(g)));
  } else {
    confBody.append(el('div', { class: 'check' },
      icon('i-ok'),
      el('div', { class: 'check-name v-green' },
        'No gaps reported — facts, frame and synthesis all resolved.'),
      el('span', { class: 'sev pass' }, 'clear')));
  }
  mount.append(panel('05', 'CONFIDENCE & GAPS', 'i-human', confBody));

  // Cross-surface linkage — refresh is handled by the freshness strip at
  // the top of the page, so this row only carries the lateral jumps.
  mount.append(el('div', { style: 'padding:0 14px 24px;display:flex;gap:8px;flex-wrap:wrap' },
    el('button', { class: 'btn ghost',
      title: 'Screen ' + a.symbol + ' deployment addresses against the OFAC SDN list',
      onclick: () => { location.hash = '#sanctions/' + a.symbol; } },
      icon('i-sanction'), 'SANCTIONS ' + a.symbol),
    el('button', { class: 'btn ghost',
      title: 'Assess ' + a.symbol + ' redemption capacity and reserve liquidity',
      onclick: () => { location.hash = '#redemptions/' + a.symbol; } },
      icon('i-redeem'), 'REDEMPTIONS ' + a.symbol)));
}

function stripCell(value, cap, cls = 'v-paper') {
  return el('div', { class: 'strip-cell' },
    el('div', { class: 'strip-val ' + cls }, value),
    el('div', { class: 'strip-cap' }, cap));
}
function mcell(label, value, cls, note, iconId, hint) {
  return el('div', { class: 'mcell' + (hint ? ' has-tip' : ''), title: hint || null },
    el('div', { class: 'mcell-label' }, icon(iconId), el('span', {}, label)),
    el('div', { class: 'mcell-val ' + cls }, value),
    note != null ? el('div', { class: 'mcell-note' }, note) : null);
}
// a quiet inline note for an n/a / perceived-gap field — gold-dotted so it
// reads as "explained, hover for why" rather than as an error.
function naFieldNote(text) {
  return el('span', { class: 'na-note' }, text);
}

function renderCheck(c) {
  const sev = c.severity || 'info';
  const iconId = c.passed ? 'i-ok' : (sev === 'critical' ? 'i-error' : sev === 'warn' ? 'i-warn' : 'i-info');
  const failCls = c.passed ? '' : ' fail-' + sev;
  // raw check id stays available on hover for traceability; the audience
  // reads the plain label.
  const titleText = checkTip(c.name) + '  ·  check: ' + String(c.name || '');
  return el('div', { class: 'check' + failCls },
    icon(iconId),
    el('div', {},
      el('div', { class: 'check-name has-tip', title: titleText },
        checkLabel(c.name)),
      c.detail ? el('div', { class: 'check-detail' }, cleanDetail(c.detail)) : null),
    el('span', { class: 'sev ' + (c.passed ? 'pass' : 'sev-' + sev) }, c.passed ? 'pass' : sev));
}

function renderGap(g) {
  if (typeof g === 'string') g = { severity: 'warn', category: 'data', message: g };
  const sev = g.severity || 'warn';
  const iconId = sev === 'critical' ? 'i-error' : sev === 'info' ? 'i-info' : 'i-warn';
  const cat = g.category || 'data';
  const why = gapWhy(cat);
  // kind-tag colour cue — an action you can take is amber-actionable; an
  // issuer limit is muted (nothing to fix on our side).
  const kindCls = why.kind === 'awaiting your action' ? 'gap-kind-action'
    : 'gap-kind-limit';
  return el('div', { class: 'gap gap-' + sev },
    icon(iconId),
    el('div', {},
      el('div', { class: 'gap-msg' }, cleanGapMessage(g.message || '')),
      // plain-language "why" — a subtle inline sub-line so a viewer can see
      // this is honest transparency, not an error.
      el('div', { class: 'gap-why' },
        el('span', { class: 'gap-why-kind ' + kindCls }, why.kind),
        el('span', { class: 'gap-why-text' }, why.text)),
      el('div', { class: 'gap-tags' },
        el('span', { class: 'gap-cat has-tip', title: why.text }, cat),
        el('span', { class: 'gap-sev sev-' + sev }, sev))));
}

// ── REASONING FRAME passage card ─────────────────────────────────────
// One cited passage = one clean card. The audience is financial /
// compliance, not engineers, so:
//   • the source title is the resolved registry name (e.g. "MiCA — Title
//     III, Article 36"), never an internal slug;
//   • the relevance score is NEVER shown — it has no scale or audience
//     meaning. It is used only to order the cards (top = highest).
//   • the body is a deterministic short summary (first sentence or ~160
//     chars, ellipsis if truncated) — no LLM, no full-text wall;
//   • "view source ↗" opens the source's canonical URL in a new tab;
//   • clicking the card expands the full passage text inline — one click
//     away when needed, not the default state.
//   • the existing human-verified / auto-included badge stays — a quality
//     signal on the citation, not a gate.
//
// `rank` (1-indexed) is supplied by the caller so the top card can carry
// a subtle visual emphasis without ever revealing a numeric score.
function renderPassage(p, rank) {
  const rankNum = Number(rank) || 0;
  const isTop = rankNum === 1;
  const text = String(p.text || '').trim();
  const summary = passageSummary(text);
  const truncated = summary && summary !== text;

  // human-verified vs auto-included — preserved from the corpus opt-out work
  const vbadge = p.source_verified
    ? el('span', { class: 'src-verified has-tip',
        title: 'A human has explicitly reviewed this source.' },
        icon('i-ok'), 'human-verified')
    : el('span', { class: 'src-auto has-tip',
        title: 'Included by default. Citable, not yet human-reviewed.' },
        'auto-included');

  // resolved source title — registry first, humanised id as fallback
  const sid = p.source_id || '';
  const title = sourceTitle(sid);
  const url = sourceUrl(sid) || p.url || '';
  const heading = (p.heading || p.section || '').trim();
  const sectionTag = p.section && p.section !== heading ? p.section : '';

  const cardCls = 'passage' + (isTop ? ' passage-top' : '');
  const card = el('div', { class: cardCls });

  // header strip — rank · source title · section heading · badge
  const head = el('div', { class: 'passage-head' });
  head.append(el('span', { class: 'passage-rank',
    title: 'Most relevant passage to this analysis' },
    String(rankNum || '·')));
  head.append(el('div', { class: 'passage-id' },
    el('span', { class: 'passage-source' }, title),
    heading ? el('span', { class: 'passage-heading' }, heading) : null,
    sectionTag ? el('span', { class: 'passage-section' }, sectionTag) : null));
  head.append(vbadge);
  card.append(head);

  // short summary — deterministic, first sentence or ~160 chars
  if (summary) {
    card.append(el('div', { class: 'passage-summary' }, summary));
  } else {
    card.append(el('div', { class: 'passage-summary passage-empty' },
      'No passage text staged for this source yet.'));
  }

  // full text — hidden by default, revealed on click
  const fullBox = el('div', { class: 'passage-full', hidden: 'hidden' },
    el('div', { class: 'passage-full-cap' }, 'FULL PASSAGE'),
    el('div', { class: 'passage-full-text' }, text || '(no text)'));
  card.append(fullBox);

  // footer — view-source link + health badge + (when there's more text) expand toggle
  const foot = el('div', { class: 'passage-foot' });
  const src = STATE.sources[p.source_id] || {};
  if (url) {
    const isBroken = src.snapshotStatus === 'broken';
    const liveHref = url;
    const snapHref = src.snapshotUrl || '';
    // When the live link is broken AND we have a snapshot, the primary
    // link points at the archived copy; the broken live URL becomes a
    // small secondary affordance.
    if (isBroken && snapHref) {
      foot.append(el('a', { class: 'passage-link passage-link-archive',
        href: snapHref, target: '_blank', rel: 'noopener noreferrer',
        title: 'live source is broken — open the archived copy we last fetched',
        onclick: (e) => e.stopPropagation() },
        'view archived copy ', el('span', { class: 'glyph' }, '⌬')));
      foot.append(el('a', { class: 'passage-link-dead',
        href: liveHref, target: '_blank', rel: 'noopener noreferrer',
        title: 'broken live URL — try the archived copy on the left',
        onclick: (e) => e.stopPropagation() },
        'live link (broken)'));
    } else {
      foot.append(el('a', { class: 'passage-link',
        href: liveHref, target: '_blank', rel: 'noopener noreferrer',
        title: 'open ' + title + ' in a new tab',
        onclick: (e) => e.stopPropagation() },
        'view source ', el('span', { class: 'glyph' }, '↗')));
    }
    if (src.snapshotStatus && src.snapshotStatus !== 'unknown') {
      foot.append(sourceHealthBadge(src));
    }
  } else {
    foot.append(el('span', { class: 'passage-link-empty' },
      'no public URL on file'));
  }
  const expandable = truncated || (text && text.length > summary.length);
  const toggle = expandable
    ? el('button', { type: 'button', class: 'passage-toggle',
        'aria-expanded': 'false',
        title: 'show full passage text inline' },
        el('span', { class: 'glyph' }, '+'),
        el('span', { class: 'passage-toggle-lbl' }, 'EXPAND'))
    : null;
  if (toggle) foot.append(toggle);
  card.append(foot);

  // click-the-card to expand — the link inside stops propagation so it
  // still opens the URL without toggling. The toggle button mirrors the
  // same action with an explicit affordance.
  const setOpen = (open) => {
    card.classList.toggle('passage-open', open);
    fullBox.hidden = !open;
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.querySelector('.glyph').textContent = open ? '−' : '+';
      toggle.querySelector('.passage-toggle-lbl').textContent =
        open ? 'COLLAPSE' : 'EXPAND';
    }
  };
  if (expandable) {
    card.classList.add('passage-clickable');
    card.addEventListener('click', (e) => {
      if (e.target.closest('a, button.passage-toggle')) return;
      setOpen(card.classList.contains('passage-open') ? false : true);
    });
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      setOpen(!card.classList.contains('passage-open'));
    });
  }
  return card;
}

// Deterministic short summary for a passage: first sentence if it fits in
// ~160 chars, else a hard cut on a word boundary with a trailing ellipsis.
// No LLM, no rewriting — this is a display-side excerpt of the staged text.
function passageSummary(text) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  if (!t) return '';
  const CAP = 160;
  if (t.length <= CAP) return t;
  // first sentence boundary — '.', '!' or '?' followed by space or end
  const m = t.match(/^(.+?[.!?])(?:\s|$)/);
  if (m && m[1].length <= CAP + 20) return m[1];
  // fall back to a clean word-boundary cut
  const cut = t.slice(0, CAP);
  const sp = cut.lastIndexOf(' ');
  return (sp > 80 ? cut.slice(0, sp) : cut).replace(/[,;:\s]+$/, '') + '…';
}

// Sort passages by score desc — highest relevance first. Stable for the
// (rare) tied case; engineering identifier (score) never reaches the DOM.
function rankPassages(passages) {
  return (passages || []).slice().sort((a, b) => {
    const sa = Number(a && a.score) || 0;
    const sb = Number(b && b.score) || 0;
    return sb - sa;
  });
}

function citeChip(citation, url, pages) {
  if (!citation) return null;
  const pageLabel = (pages && pages.length) ? ' · p.' + pages.join(', ') : '';
  const inner = [el('span', { class: 'glyph' }, '§'),
    el('span', {}, citation + pageLabel)];
  if (url) return el('a', { class: 'cite', href: url, target: '_blank', rel: 'noopener' }, ...inner);
  return el('span', { class: 'cite', title: 'Citation reference (not resolvable to a URL)' }, ...inner);
}

// ── narrative emoji scrub ────────────────────────────────────────────
// LLM-generated narratives use raw emoji as status markers. Raw emoji are
// strictly off-brand — none may ever render. Status emoji are mapped to
// the exact brand SVG sprites used by the guardrail-check rows (renderCheck:
// i-ok / i-error / i-warn / i-info); every other emoji is stripped.
//
// Each status emoji is first swapped for an ASCII sentinel token BEFORE
// markdown escaping, then the token is replaced with inline SVG markup
// AFTER the HTML string is assembled — so the SVG survives esc() untouched
// and renders correctly inside paragraphs and table cells.
// Placeholders are plain ASCII sentinels that never appear in real prose
// and pass through esc() unchanged. An optional trailing variation
// selector (U+FE0F) is consumed with the emoji so no stray glyph survives.
const EMOJI_ICON = [
  // pass — green
  { re: /[✅✔✓☑\u{1F7E2}\u{1F7E9}]️?/gu,
    ph: '@@MDIC-OK@@', id: 'i-ok' },
  // fail / cross — rose
  { re: /[❌✗✘\u{1F534}\u{1F7E5}]️?/gu,
    ph: '@@MDIC-ERR@@', id: 'i-error' },
  // warning — amber
  { re: /[⚠\u{1F7E1}\u{1F7E0}]️?/gu,
    ph: '@@MDIC-WARN@@', id: 'i-warn' },
  // info — gold
  { re: /[ℹ\u{1F535}]️?/gu,
    ph: '@@MDIC-INFO@@', id: 'i-info' },
];
// broad catch-all for any OTHER emoji (pictographs, symbols, flags, ZWJ
// sequences, skin tones, variation selectors) — stripped entirely so an
// unmapped emoji can never reach the screen.
const EMOJI_STRIP = new RegExp(
  '[\\u200D\\uFE0F\\u20E3'
  + '\\u{1F000}-\\u{1FAFF}\\u{1F1E6}-\\u{1F1FF}'
  + '\\u{2600}-\\u{27BF}\\u{2B00}-\\u{2BFF}'
  + '\\u{2300}-\\u{23FF}\\u{1F3FB}-\\u{1F3FF}'
  + '\\u2705\\u274C\\u26A0]', 'gu');

// map status emoji to their placeholder tokens — run on the RAW source,
// before escaping. Leaves a placeholder the inline() escape ignores.
function tagEmoji(src) {
  let s = String(src);
  for (const m of EMOJI_ICON) s = s.replace(m.re, m.ph);
  return s;
}
// after the HTML is assembled: placeholders → brand SVG; then strip any
// remaining (unmapped) emoji and tidy the leftover double spacing.
function resolveEmoji(html) {
  let h = html;
  for (const m of EMOJI_ICON) {
    h = h.split(m.ph).join(
      '<svg class="md-ic" viewBox="0 0 24 24" aria-hidden="true">'
      + '<use href="#' + m.id + '"></use></svg>');
  }
  h = h.replace(EMOJI_STRIP, '');
  // tidy whitespace left by a stripped emoji — collapse runs, drop a space
  // left dangling before punctuation or a closing block tag.
  h = h.replace(/ {2,}/g, ' ')
       .replace(/ +([.,;:!?])/g, '$1')
       .replace(/ +(<\/(?:p|li|td|th|h[1-3])>)/g, '$1');
  return h;
}

// ── minimal markdown renderer for the narrative ──────────────────────
function markdown(src) {
  if (!src) return '';
  const lines = tagEmoji(String(src)).split('\n');
  let html = '', list = null;
  const inline = (t) => esc(t)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // inline [tool:X] provenance markers → a subtle muted provenance chip.
    // The raw marker stays in the underlying data; this is display only.
    .replace(/\[tool:([a-z_]+)\]/gi, (_, id) =>
      '<span class="prov-chip" title="Provenance — where this figure '
      + 'comes from">' + esc(toolProvenance(id)) + '</span>')
    // corpus citations [slug §Section] → a clean regulatory source label.
    .replace(/\[([a-z0-9][a-z0-9-]+)\s*§\s*([^\]]+)\]/gi, (_, slug, section) =>
      '<span class="src-cite" title="Regulatory source">'
      + esc(corpusSourceName(slug)) + ' &mdash; ' + esc(section.trim())
      + '</span>');
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const isTableRow = (s) => /^\s*\|.*\|\s*$/.test(s);
  const isDivider = (s) => /^\s*\|?[\s:|-]+\|?\s*$/.test(s) && s.includes('-');
  const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  const nextContent = (j) => { while (j < lines.length && !lines[j].trim()) j++; return j; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\s+$/, '');
    const divIdx = nextContent(i + 1);
    if (isTableRow(line) && divIdx < lines.length && isDivider(lines[divIdx])) {
      closeList();
      const head = cells(line);
      let body = '';
      let j = nextContent(divIdx + 1);
      while (j < lines.length && isTableRow(lines[j])) {
        body += '<tr>' + cells(lines[j]).map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>';
        j = nextContent(j + 1);
      }
      i = j - 1;
      html += '<table class="md-table"><thead><tr>' +
        head.map((c) => `<th>${inline(c)}</th>`).join('') +
        '</tr></thead><tbody>' + body + '</tbody></table>';
      continue;
    }
    if (!line.trim()) { closeList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)/))) {
      closeList();
      html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`;
    } else if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
      if (list !== 'ul') { closeList(); list = 'ul'; html += '<ul>'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)/))) {
      if (list !== 'ol') { closeList(); list = 'ol'; html += '<ol>'; }
      html += `<li>${inline(m[1])}</li>`;
    } else {
      closeList();
      html += `<p>${inline(line)}</p>`;
    }
  }
  closeList();
  // resolve status-emoji placeholders → brand SVG icons and strip any
  // other (unmapped) emoji so no raw emoji can ever reach the screen.
  return resolveEmoji(html);
}

// ════════════════════════════════════════════════════════════════════
//  COMPLIANCE SURFACES — F5 SANCTIONS · F6 REDEMPTIONS
//  Both mirror the ANALYZE async-job pattern: POST starts a job, GET
//  polls. Slow Python (SDN-list load / attestation fetch + LLM) runs in
//  a server background thread while the UI shows staged progress.
// ════════════════════════════════════════════════════════════════════
let surfacePollTimer = null;

// place the instrument's brand mark in any surface view header
function setSurfaceHeaderMark(symbol) {
  const head = $('.view-head');
  if (!head) return;
  const existing = $('.view-mark', head);
  if (existing) existing.remove();
  if (!symbol) return;
  const tag = $('.view-tag', head);
  head.insertBefore(tokenMark(symbol, 'view-mark'), tag.nextSibling);
}

// a compact per-chain supply table — shared by both surfaces. A leaner
// echo of the ANALYZE per-chain breakdown so each surface still shows
// the deployments its facts were read from.
function supplyChainTable(supply) {
  if (!(supply.per_chain || []).length) return null;
  const rows = supply.per_chain.map((c) => {
    const isBridged = c.kind === 'bridged';
    const verified = c.verified !== false;
    const expUrl = explorerUrl(c.chain, c.contract);
    const addrCell = expUrl
      ? el('a', { class: 'addr addr-link', href: expUrl,
          target: '_blank', rel: 'noopener noreferrer',
          title: 'open contract on ' + c.chain + ' explorer' },
          el('span', {}, c.contract), el('span', { class: 'addr-ext' }, '↗'))
      : el('span', { class: 'addr' }, c.contract);
    return el('tr', {},
      el('td', {}, el('span', { class: 'chain-id' },
        chainMark(c.chain, 'cmark-tbl'), c.chain)),
      el('td', {}, addrCell),
      el('td', {}, el('span', {
        class: 'kind has-tip kind-' + (isBridged ? 'bridged' : 'native'),
        title: isBridged ? tip.kindBridged : tip.kindNative },
        isBridged ? 'bridged' : 'native')),
      el('td', {}, el('span', {
        class: 'vmark has-tip ' + (verified ? 'vmark-ok' : 'vmark-no'),
        title: verified ? tip.verified : tip.unverified },
        icon(verified ? 'i-ok' : 'i-warn'),
        verified ? 'verified' : 'unverified')),
      el('td', { class: 'num ' + (isBridged ? 'v-amber' : 'v-paper') },
        fmtNum(c.supply, 0)));
  });
  return el('table', { class: 'dtable' },
    el('thead', {}, el('tr', {},
      el('th', {}, 'CHAIN'), el('th', {}, 'CONTRACT'),
      el('th', { title: tip.kindNative + '  /  ' + tip.kindBridged }, 'KIND'),
      el('th', { title: tip.verified }, 'VERIFIED'),
      el('th', { class: 'num' }, 'SUPPLY'))),
    el('tbody', {}, ...rows));
}

// the GUARDRAIL CHECKS panel — reused renderCheck(), shared by surfaces
function checksPanel(num, checks) {
  checks = checks || [];
  return panel(num, `GUARDRAIL CHECKS · ${checks.length}`, 'i-gate',
    checks.length
      ? el('div', {}, ...checks.map(renderCheck))
      : el('div', { class: 'empty' }, icon('i-gate'),
          el('b', {}, 'No checks ran.')));
}

// the REASONING FRAME panel — reused renderPassage(), shared by surfaces
function passagesPanel(num, passages) {
  const ranked = rankPassages(passages);
  return panel(num, `REASONING FRAME · APPROVED CORPUS · ${ranked.length}`,
    'i-frame',
    ranked.length
      ? el('div', { class: 'passage-list' },
          ...ranked.map((p, i) => renderPassage(p, i + 1)))
      : el('div', { class: 'empty' }, icon('i-frame'),
          el('b', {}, 'No corpus passages retrieved'),
          el('div', {}, 'Judgements remain unsupported until source text '
            + 'is staged and ingested.')));
}

// the GAPS & OPEN ITEMS panel — reused renderGap(), shared by surfaces
function gapsPanel(num, gaps) {
  gaps = gaps || [];
  const body = el('div', {});
  body.append(el('div', { class: 'sub-head' },
    `GAPS & OPEN ITEMS · ${gaps.length}`));
  if (gaps.length) {
    gaps.forEach((g) => body.append(renderGap(g)));
  } else {
    body.append(el('div', { class: 'check' },
      icon('i-ok'),
      el('div', { class: 'check-name v-green' },
        'No gaps reported — facts, frame and synthesis all resolved.'),
      el('span', { class: 'sev pass' }, 'clear')));
  }
  return panel(num, 'CONFIDENCE & GAPS', 'i-human', body);
}

// a tier badge — liquid green · moderate amber · illiquid rose
function tierBadge(t) {
  const key = String(t || '').toLowerCase().trim();
  const cls = key === 'liquid' ? 'tier-liquid'
    : key === 'moderate' ? 'tier-moderate'
    : key === 'illiquid' ? 'tier-illiquid' : 'tier-moderate';
  return el('span', { class: 'tier has-tip ' + cls, title: tip.tier },
    key || 'untiered');
}

// ── the shared surface view shell + async job runner ─────────────────
// kind: 'sanctions' | 'redemptions'. cfg holds the per-surface labels,
// API path, log tag, and the completed-result renderer.
const SURFACES = {
  sanctions: {
    tag: 'F5', title: 'SANCTIONS',
    sub: 'OFAC SDN screening of token deployment addresses',
    api: '/sanctions', logTag: 'SANCTIONS', icon: 'i-sanction',
    runLabel: 'SCREEN', render: renderSanctions,
    emptyHead: 'Select an instrument',
    emptyBody: 'Pick a token from the sidebar, or type a symbol and SCREEN. '
      + 'The agent reads live deployments, loads the OFAC SDN list, screens '
      + 'every address, runs guardrails, and synthesises a cited screen.',
  },
  redemptions: {
    tag: 'F6', title: 'REDEMPTIONS',
    sub: 'redemption capacity · reserve liquidity tiered against on-chain supply',
    api: '/redemption', logTag: 'REDEEM', icon: 'i-redeem',
    runLabel: 'ASSESS', render: renderRedemption,
    emptyHead: 'Select an instrument',
    emptyBody: 'Pick a token from the sidebar, or type a symbol and ASSESS. '
      + 'The agent reads live supply, extracts the attestation, classifies '
      + 'reserves into liquidity tiers, runs guardrails, and synthesises a '
      + 'cited redemption assessment.',
  },
};

function viewSurface(kind, symbolFromHash) {
  const cfg = SURFACES[kind];
  app.innerHTML = '';
  _feedRendered = STATE.feed.length;

  const input = el('input', {
    class: 'tinput', type: 'text', placeholder: 'SYMBOL',
    autocomplete: 'off', spellcheck: 'false',
    value: symbolFromHash || STATE.activeSymbol || '',
  });
  const runBtn = el('button', { class: 'btn' }, icon(cfg.icon), cfg.runLabel);
  app.append(viewHead(cfg.tag, cfg.title, cfg.sub, input, runBtn));

  const mount = el('div', { class: 'view-body', id: 'sf-mount' });
  app.append(mount);

  const run = () => {
    const sym = input.value.trim().toUpperCase();
    if (!sym) { flagEmptyInput(input); return; }
    // Always trigger a run — when the hash already equals the target no
    // `hashchange` fires, so invoke the job path directly in that case.
    const target = '#' + kind + '/' + sym;
    if (location.hash === target) startSurfaceJob(kind, sym, mount);
    else location.hash = target;
  };
  runBtn.addEventListener('click', run);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });

  // Resolve the token to run. An explicit hash symbol wins; otherwise fall
  // back to whatever instrument is currently in context (last analysed /
  // screened). Either way the surface runs straight away — no dead-end.
  const symbol = symbolFromHash || STATE.activeSymbol || '';
  if (symbol) {
    // Cached-first load: render the last completed result instantly if
    // the Store has one, regardless of age. Only on a true miss do we
    // start the job spinner.
    const surfaceKey = (kind === 'sanctions') ? 'sanctions' : 'redemption';
    renderCachedOrRun(surfaceKey, symbol, mount,
                      () => startSurfaceJob(kind, symbol, mount, true),
                      (sym, m) => startSurfaceJob(kind, sym, m));
  } else {
    // genuinely no token in context — render an in-view instrument picker
    // instead of a terse error. Picking a row runs the surface in place.
    mount.append(surfacePicker(kind, mount, input));
  }
}

// ── in-view instrument picker — the F5/F6 no-token landing state ─────
// A clean, terminal-styled prompt followed by the tracked stablecoins as
// clickable rows (same token marks/styling as the sidebar). Clicking one
// runs the surface for that token in place — never bounces out of the view.
function surfacePicker(kind, mount, input) {
  const cfg = SURFACES[kind];
  const pickVerb = kind === 'sanctions' ? 'screen' : 'assess';
  const body = el('div', {});

  // intro line — intentional landing copy, not an error message
  body.append(el('div', { class: 'picker-intro' },
    icon(cfg.icon),
    el('div', {},
      el('div', { class: 'picker-head' },
        'Select an instrument to ' + pickVerb),
      el('div', { class: 'picker-sub' }, cfg.emptyBody))));

  const grid = el('div', { class: 'picker-grid' });
  const pick = (sym) => {
    // run in place — update the header SYMBOL input + hash, then start
    if (input) input.value = sym;
    if (location.hash !== '#' + kind + '/' + sym) {
      location.hash = '#' + kind + '/' + sym;
    } else {
      startSurfaceJob(kind, sym, mount);
    }
  };

  if (STATE.tokens.length) {
    STATE.tokens.forEach((t) => {
      const s = STATE.supply[t.symbol];
      const st = tokenStatus(s);
      grid.append(el('div', {
        class: 'picker-card click',
        title: cfg.runLabel + ' ' + t.symbol,
        onclick: () => pick(t.symbol),
      },
        el('div', { class: 'picker-card-id' },
          tokenMark(t.symbol, 'tmark-side'),
          el('div', { style: 'min-width:0' },
            el('div', { class: 'instr-sym' }, t.symbol),
            el('div', { class: 'instr-sub' }, t.issuer))),
        el('div', { class: 'picker-card-go' },
          el('span', {
            class: 'picker-stat v-' + ({ ok: 'green', watch: 'amber',
              alert: 'rose', idle: 'muted' }[st.tag]) },
            st.label),
          el('span', { class: 'picker-run' },
            cfg.runLabel, el('span', { class: 'picker-arrow' }, '→')))));
    });
  } else {
    grid.append(el('div', { class: 'empty' },
      icon(cfg.icon),
      el('b', {}, 'Loading instrument registry…')));
  }
  body.append(grid);

  return panel(null, 'SELECT INSTRUMENT · ' + cfg.title, cfg.icon, body, true);
}

async function startSurfaceJob(kind, symbol, mount, refresh = false) {
  const cfg = SURFACES[kind];
  if (surfacePollTimer) clearInterval(surfacePollTimer);
  STATE.activeSymbol = symbol;
  setSurfaceHeaderMark(symbol);
  refreshInstruments();
  mount.innerHTML = '';

  logLine('WORK', cfg.logTag, [
    seg(symbol.padEnd(5), 'lg-sym'),
    seg('job queued', 'lg-val'),
    seg(refresh ? 'mode=refresh' : 'mode=cached',
      refresh ? 'd-warn' : 'd-up'),
  ]);

  let job;
  try {
    const tier = getTier();
    if (tier === 'deep') startDeepCountdown(Date.now() + 120000);
    job = await api(cfg.api, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, refresh, tier }),
    });
    if (job.status === 'done') stopDeepCountdown();
  } catch (e) {
    logLine('ERR', cfg.logTag, [
      seg(symbol.padEnd(5), 'lg-sym'), seg('START-FAIL', 'd-warn'),
      seg(String(e.message).slice(0, 48)),
    ]);
    mount.append(errorBox('Could not start ' + cfg.title.toLowerCase()
      + ' · ' + symbol, e.message, undefined,
      { onRetry: () => startSurfaceJob(kind, symbol, mount, true),
        retryLabel: 'TRY AGAIN' }));
    return;
  }

  // Cached hit — already computed and still fresh. Render instantly, no
  // job to poll and no staged motions.
  if (job.status === 'done') {
    logLine('OK', cfg.logTag, [
      seg(symbol.padEnd(5), 'lg-sym'), seg('cached', 'd-up'),
    ]);
    mount.innerHTML = '';
    cfg.render(job.result, null, mount, job.computed_at,
      () => startSurfaceJob(kind, symbol, mount, true));
    return;
  }

  const stages = job.stages || [];
  const t0 = Date.now();
  const stageNodes = stages.map((label, i) =>
    el('div', { class: 'rstage', 'data-i': i },
      el('div', { class: 'rs-mark' }),
      el('div', { class: 'rs-text' },
        el('div', { class: 'rs-label' }, label),
        el('div', { class: 'rs-sub' }, '')),
      el('span', { class: 'rs-tk' }, 'S' + pad2(i + 1))));

  const surfaceKey = (kind === 'sanctions') ? 'sanctions' : 'redemption';
  const expected = expectedLatency(surfaceKey, symbol);
  const waitHint = el('div', { class: 'rs-wait' },
    el('span', { class: 'rs-wait-mark' }, '◇'),
    el('span', { class: 'rs-wait-txt' }, latencyHint(surfaceKey, symbol)));
  const scanbar = el('div', { class: 'scanbar' });
  const progress = panel(null,
    'JOB ' + job.job_id.toUpperCase() + ' · ' + symbol, cfg.icon,
    el('div', {},
      el('div', { class: 'run-stages' }, ...stageNodes),
      scanbar,
      waitHint));
  $('.panel-head', progress).insertBefore(
    el('span', { class: 'rs-tk', style: 'margin-left:auto' }, ''),
    $('.panel-ic', progress));
  const clockHost = $('.panel-head .rs-tk', progress);
  mount.append(progress);

  const SUB_NARRATION = stageSubNarration(stages);
  let stage = 0;
  let subIdx = 0;
  let subTimer = null;
  const cycleSub = () => {
    const lines = SUB_NARRATION[stage] || [];
    if (!lines.length) return;
    const node = stageNodes[stage] && stageNodes[stage].querySelector('.rs-sub');
    if (!node) return;
    node.textContent = lines[subIdx % lines.length];
    node.classList.remove('rs-sub-tick');
    void node.offsetWidth;
    node.classList.add('rs-sub-tick');
    subIdx++;
  };
  const advance = () => {
    stageNodes.forEach((n, i) => {
      n.className = 'rstage'
        + (i < stage ? ' done' : i === stage ? ' active' : '');
      const sub = n.querySelector('.rs-sub');
      if (sub && i !== stage) sub.textContent = '';
    });
    subIdx = 0;
    cycleSub();
  };
  advance();
  if (subTimer) clearInterval(subTimer);
  subTimer = setInterval(cycleSub, 1700);
  let lastLogged = -1;
  const stageTimer = setInterval(() => {
    if (stage < stages.length - 1) { stage++; advance(); }
  }, 5200);
  const clockTimer = setInterval(() => {
    const elapsedS = Math.round((Date.now() - t0) / 1000);
    const over = elapsedS > expected.seconds * 1.5;
    const wayOver = elapsedS > expected.seconds * 2.5;
    clockHost.textContent = over
      ? elapsedS + 's'
      : elapsedS + 's / ~' + expected.seconds + 's';
    clockHost.className = 'rs-tk' + (wayOver ? ' rs-tk-slow' :
      over ? ' rs-tk-overrun' : '');
    if (wayOver) {
      waitHint.classList.add('rs-wait-slow');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'Taking longer than usual. Still working — most often a slow '
        + 'issuer page or LLM response.';
    } else if (over) {
      waitHint.classList.add('rs-wait-over');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'A little past the typical ' + expected.seconds + 's, still on track.';
    }
    const pct = Math.min(96, Math.round((elapsedS / expected.seconds) * 100));
    scanbar.style.setProperty('--scan-pct', pct + '%');
    const s = elapsedS + 's';
    if (stage !== lastLogged) {
      lastLogged = stage;
      logLine('WORK', cfg.logTag, [
        seg(symbol.padEnd(5), 'lg-sym'),
        seg('stage ' + (stage + 1) + '/' + stages.length, 'lg-val'),
        seg(s.padStart(4)),
        seg(stages[stage]),
      ]);
    }
  }, 250);

  const finish = () => {
    clearInterval(stageTimer); clearInterval(clockTimer);
    clearInterval(surfacePollTimer); surfacePollTimer = null;
    if (subTimer) { clearInterval(subTimer); subTimer = null; }
  };

  surfacePollTimer = setInterval(async () => {
    if (STATE.activeSymbol !== symbol) {
      // Hand off to the background tracker so a toast lands when this
      // backgrounded sanctions / redemption run finishes.
      trackJob(job.job_id, kind, symbol);
      finish();
      return;
    }
    let st;
    try { st = await api(cfg.api + '/' + job.job_id); }
    catch (e) {
      finish(); mount.innerHTML = '';
      logLine('ERR', cfg.logTag, [
        seg(symbol.padEnd(5), 'lg-sym'), seg('POLL-FAIL', 'd-warn'),
        seg(String(e.message).slice(0, 48)),
      ]);
      mount.append(errorBox('Polling failed · ' + symbol, e.message,
        undefined,
        { onRetry: () => startSurfaceJob(kind, symbol, mount, true),
          retryLabel: 'TRY AGAIN' }));
      return;
    }
    if (st.status === 'running') return;
    finish();
    stage = stages.length; advance();
    mount.innerHTML = '';
    stopDeepCountdown();
    if (st.status === 'error') {
      logLine('ERR', cfg.logTag, [
        seg(symbol.padEnd(5), 'lg-sym'), seg('FAILED', 'd-warn'),
        seg(String(st.error).slice(0, 56)),
      ]);
      mount.append(errorBox(cfg.title + ' failed · ' + symbol,
        st.error, st.trace,
        { onRetry: () => startSurfaceJob(kind, symbol, mount, true),
          retryLabel: 'TRY AGAIN' }));
    } else {
      const sk = (kind === 'sanctions') ? 'sanctions' : 'redemption';
      recordLatency(sk, symbol, st.elapsed);
      cfg.render(st.result, st.elapsed, mount,
        new Date().toISOString(),
        () => startSurfaceJob(kind, symbol, mount, true));
    }
  }, 1400);
}

// ── render a completed SanctionsScreen ───────────────────────────────
function renderSanctions(s, elapsed, mount, computedAt, onRefresh) {
  const supply = s.supply || {};
  const hits = s.hits || [];
  const screened = s.screened || [];
  const clean = hits.length === 0;

  if (computedAt !== undefined && onRefresh) {
    mount.append(freshnessStrip(computedAt, onRefresh));
  }

  logLine(clean ? 'OK' : 'ALERT', 'SANCTIONS', [
    seg((s.symbol || '').padEnd(5), 'lg-sym'),
    seg('done ' + (elapsed != null ? elapsed + 's' : ''), 'lg-val'),
    seg('scr' + screened.length),
    seg(clean ? 'no SDN hits' : hits.length + ' SDN HIT(S)',
      clean ? 'd-up' : 'd-dn'),
  ]);

  // summary strip
  mount.append(el('div', { class: 'strip fade-in' },
    stripCell(s.symbol, 'TOKEN', 'v-gold'),
    stripCell(String(screened.length), 'ADDRESSES SCREENED'),
    stripCell(clean ? 'CLEAR' : String(hits.length) + ' HIT',
      'OFAC SDN RESULT', clean ? 'v-green' : 'v-rose'),
    stripCell(elapsed != null ? elapsed + 's' : '—', 'RUN TIME'),
  ));

  // ── 01 SDN SCREEN RESULT ──
  const screenBody = el('div', {});
  // Backing-model strip — same placement as ANALYZE: the lineage of every
  // figure on this surface is named before any number. Surfaces the
  // 'BACKING MODEL' badge + protocol-docs link.
  screenBody.append(backingModelStrip(s));
  if (clean) {
    // a clean screen is the norm — render it as a clear green pass
    screenBody.append(el('div', { class: 'clean-state' },
      icon('i-ok'),
      el('div', {},
        el('div', { class: 'cs-head' }, 'NO OFAC SDN MATCHES'),
        el('div', { class: 'cs-sub' },
          'Every screened deployment address was checked against the OFAC '
          + 'Specially Designated Nationals list and matched none. This is '
          + 'the expected, healthy state.'))));
  } else {
    screenBody.append(el('div', { class: 'sub-head' },
      `OFAC SDN MATCHES · ${hits.length}`));
    hits.forEach((h) => screenBody.append(el('div', { class: 'hit-row' },
      icon('i-error'),
      el('div', {},
        el('div', { class: 'hit-name' },
          (h.sdn_name || 'sanctioned entity')),
        el('div', { class: 'hit-meta' },
          el('span', { class: 'addr' }, h.address || ''),
          '  ·  ' + (h.currency || '?') + '  ·  SDN UID '
            + (h.sdn_uid || '—'))))));
  }

  // SDN list provenance — publish date, staleness, address-universe size
  screenBody.append(el('div', { class: 'sub-head' }, 'OFAC SDN LIST'));
  screenBody.append(el('div', { class: 'mgrid', style: 'border-top:none' },
    mcell('SDN PUBLISH DATE', s.sdn_publish_date || 'n/a', 'v-paper',
      'when the SDN list was last published', 'i-doc'),
    mcell('SDN STALENESS',
      s.sdn_staleness_days != null ? s.sdn_staleness_days + ' days' : 'n/a',
      s.sdn_staleness_days != null && s.sdn_staleness_days > 7
        ? 'v-amber' : 'v-paper',
      'age of the loaded SDN list', 'i-gate', tip.sdnStaleness),
    mcell('SANCTIONED ADDRESSES', fmtNum(s.sdn_address_count, 0), 'v-gold',
      'crypto addresses on the SDN list', 'i-metric', tip.sdnCount),
  ));

  // screened addresses
  if (screened.length) {
    screenBody.append(el('div', { class: 'sub-head has-tip',
      title: tip.screened }, 'SCREENED ADDRESSES'));
    const rows = screened.map((addr) => el('tr', {},
      el('td', {}, el('span', { class: 'addr' }, addr)),
      el('td', {}, el('span', {
        class: 'vmark vmark-ok' }, icon('i-ok'), 'no SDN match'))));
    screenBody.append(el('table', { class: 'dtable' },
      el('thead', {}, el('tr', {},
        el('th', { title: tip.screened }, 'ADDRESS'),
        el('th', {}, 'SCREEN RESULT'))),
      el('tbody', {}, ...rows)));
  }

  // deployment provenance
  const chainTbl = supplyChainTable(supply);
  if (chainTbl) {
    screenBody.append(el('div', { class: 'sub-head' },
      'TOKEN DEPLOYMENTS SCREENED'));
    screenBody.append(chainTbl);
  }
  if (supply.read_at) {
    screenBody.append(el('div', { class: 'prov-stamp' },
      el('span', { class: 'glyph' }, '§'),
      'on-chain reads taken at ' + supply.read_at));
  }
  // AI-context cards — qualitative fill-in when the SDN screen couldn't
  // run cleanly (list unavailable, critically stale, or no addresses to
  // screen). Tagged 'AI CONTEXT' and visually distinct from a real hit.
  (s.augmentations || []).forEach((ctx) =>
    screenBody.append(augmentationCard(ctx)));
  mount.append(panel('01', 'SDN SCREEN', 'i-sanction', screenBody));

  // ── 02 GUARDRAIL CHECKS · 03 REASONING FRAME · 04 NARRATIVE · 05 GAPS ──
  mount.append(checksPanel('02', s.checks));
  mount.append(passagesPanel('03', s.passages));
  mount.append(panel('04', 'SCREEN NARRATIVE', 'i-agent',
    s.narrative
      ? el('div', { class: 'narrative', html: markdown(s.narrative) })
      : el('div', { class: 'empty' }, icon('i-agent'),
          el('b', {}, 'No narrative synthesised.'))));
  mount.append(gapsPanel('05', s.gaps));

  mount.append(el('div', { style: 'padding:0 14px 24px;display:flex;gap:8px;flex-wrap:wrap' },
    el('button', { class: 'btn ghost',
      onclick: () => { location.hash = '#analyze/' + s.symbol; } },
      icon('i-agent'), 'ANALYZE ' + s.symbol),
    el('button', { class: 'btn ghost',
      onclick: () => { location.hash = '#redemptions/' + s.symbol; } },
      icon('i-redeem'), 'REDEMPTIONS ' + s.symbol)));
}

// ── render a completed RedemptionAssessment ──────────────────────────
function renderRedemption(r, elapsed, mount, computedAt, onRefresh) {
  const supply = r.supply || {};
  const att = r.attestation;
  const m = r.metrics;
  const tiers = r.tiers || [];
  const flow = r.net_redemption_flow;

  if (computedAt !== undefined && onRefresh) {
    mount.append(freshnessStrip(computedAt, onRefresh));
  }

  logLine('OK', 'REDEEM', [
    seg((r.symbol || '').padEnd(5), 'lg-sym'),
    seg('done ' + (elapsed != null ? elapsed + 's' : ''), 'lg-val'),
    seg('liq' + (r.liquid_coverage != null
      ? fmtPct(r.liquid_coverage) : 'n/a'),
      r.liquid_coverage != null ? covClass(r.liquid_coverage) : ''),
    seg('tiers' + tiers.length),
  ]);

  // Summary strip — the "as of" cell adapts to the backing model so it
  // shows a meaningful date for crypto-collateral / synthetic /
  // algorithmic tokens (the on-chain read time) rather than a useless
  // dash. ATTESTED AS OF still applies for fiat-backed CPA reports.
  const isFiat = (r.backing_model || 'fiat_reserves') === 'fiat_reserves';
  const asOfLabel = isFiat ? 'ATTESTED AS OF' : 'ON-CHAIN AS OF';
  const asOfValue = att ? att.as_of_date
    : (supply.read_at ? supply.read_at.slice(0, 10) : '—');
  mount.append(el('div', { class: 'strip fade-in' },
    stripCell(r.symbol, 'TOKEN', 'v-gold'),
    stripCell(asOfValue, asOfLabel),
    stripCell(fmtUSD(supply.total_supply), 'ON-CHAIN · NATIVE'),
    stripCell(elapsed != null ? elapsed + 's' : '—', 'RUN TIME'),
  ));

  // ── 01 REDEMPTION SNAPSHOT ──
  const naLiq = r.liquid_coverage == null;
  const snapBody = el('div', {});
  // Backing-model strip — names the lineage of every figure on this
  // surface before any number. Lets the reader see at a glance that a
  // crypto-collateralized token has no fiat tier breakdown by design.
  snapBody.append(backingModelStrip(r));

  // ── When the deterministic pipeline could not resolve an attestation,
  // the LLM augmentation card becomes the PRIMARY content. The bare
  // n/a fields move into a collapsed disclosure for trace-readers; the
  // user-facing answer is what the augmentation provides (with
  // citations). This implements the standing rule: never let bare n/a
  // sit front and centre when we have ANY contextual answer to give.
  const augCards = r.augmentations || [];
  if (!att && augCards.length > 0) {
    augCards.forEach((ctx) => snapBody.append(augmentationCard(ctx)));
    // Always-on supply (on-chain) stays visible — it's the one figure
    // the deterministic pipeline DID verify.
    snapBody.append(el('div', { class: 'mgrid' },
      mcell('ON-CHAIN SUPPLY · NATIVE', fmtNum(supply.total_supply, 0),
        'v-paper',
        `${(supply.per_chain || []).length} deployment(s) · excludes bridged`,
        'i-supply'),
    ));
    // Reserve detail fields under a quiet disclosure (same pattern as
    // analyze): the AI Context above already serves the answer; the
    // deterministic placeholders are tucked here for trace-readers, and
    // a small footnote names why the raw extractor came up empty.
    const naDetails = el('details', { class: 'na-disclosure' });
    naDetails.append(el('summary', {},
      el('span', { class: 'na-disc-glyph' }, '⊕'),
      el('b', {}, 'Reserve detail fields '),
      el('span', { class: 'na-disc-hint' },
        '(coverage ratios, reserve breakdown, staleness; populated when '
        + 'a fresh CPA attestation PDF resolves)')));
    const inner = el('div', { class: 'na-disc-body' });
    inner.append(el('div', { class: 'cov-row' },
      el('div', { class: 'cov-cell' },
        el('div', { class: 'cov-kick' }, 'LIQUID COVERAGE'),
        el('div', { class: 'cov-big v-paper' }, '—'),
        el('div', { class: 'cov-desc' },
          'Liquid reserves ÷ current on-chain supply. The AI Context '
          + 'above carries the qualitative picture; this populates when '
          + 'a fresh attestation PDF resolves.')),
      el('div', { class: 'cov-cell' },
        el('div', { class: 'cov-kick' }, 'LIVE COVERAGE'),
        el('div', { class: 'cov-big v-paper' }, '—'),
        el('div', { class: 'cov-desc' },
          'Total attested reserves ÷ current on-chain supply. '
          + 'Populates when a fresh attestation PDF resolves.')),
    ));
    inner.append(el('div', { class: 'mgrid' },
      mcell('ATTESTED RESERVES', '—', 'v-paper',
        'see AI Context above', 'i-doc'),
      mcell('LIQUID RESERVES', '—', 'v-muted',
        'fast-redeemable assets from the attestation breakdown',
        'i-metric'),
      mcell('NET REDEMPTION FLOW', '—', 'v-paper',
        'on-chain supply minus attested tokens outstanding', 'i-metric'),
      mcell('STALENESS', '—', 'v-paper',
        'age of the attestation', 'i-gate'),
    ));
    inner.append(el('div', { class: 'na-disc-footnote' },
      el('span', { class: 'glyph' }, '§'),
      'Automated fetch returned no PDF this run — most often the issuer '
      + 'page is JavaScript-rendered, the attestation URL rotated, or '
      + 'the document is gated behind a SPA. Background discovery '
      + 'retries every six hours; RE-RUN above for an immediate refresh.'));
    naDetails.append(inner);
    snapBody.append(naDetails);
  } else {
    // Normal path: attestation resolved (or backing-model-by-design n/a).
    snapBody.append(el('div', { class: 'cov-row' },
      el('div', { class: 'cov-cell has-tip', title: tip.liquidCoverage },
        el('div', { class: 'cov-kick' },
          'LIQUID COVERAGE — FAST REDEMPTION CAPACITY'),
        el('div', { class: 'cov-big ' + covClass(r.liquid_coverage) },
          naLiq ? 'n/a' : fmtPct(r.liquid_coverage)),
        el('div', { class: 'cov-desc' }, naLiq
          ? naFieldNote('No current attestation could be resolved — see GAPS '
              + 'below. On-chain supply is unaffected.')
          : 'Liquid reserves ÷ current on-chain supply. The share of '
            + 'circulating supply redeemable using only fast-access assets.')),
      el('div', { class: 'cov-cell has-tip',
        title: m ? tip.live : tip.na.coverage },
        el('div', { class: 'cov-kick' }, 'LIVE COVERAGE — TOTAL RESERVES'),
        el('div', { class: 'cov-big ' + covClass(m && m.live_coverage) },
          m ? fmtPct(m.live_coverage) : 'n/a'),
        el('div', { class: 'cov-desc' }, m
          ? 'Total attested reserves ÷ current on-chain supply — all tiers, '
            + 'not just the liquid ones.'
          : naFieldNote('No current attestation could be resolved — see GAPS '
              + 'below.'))),
    ));

    snapBody.append(el('div', { class: 'mgrid' },
      mcell('ON-CHAIN SUPPLY · NATIVE', fmtNum(supply.total_supply, 0),
        'v-paper',
        `${(supply.per_chain || []).length} deployment(s) · excludes bridged`,
        'i-supply'),
      mcell('ATTESTED RESERVES', att ? fmtUSD(att.total_reserves) : 'n/a',
        'v-gold',
        att ? 'from the latest attestation'
            : naFieldNote('no attestation resolved — hover'),
        'i-doc', att ? null : tip.na.reserves),
      mcell('LIQUID RESERVES', fmtUSD(r.liquid_reserves),
        r.liquid_reserves > 0 ? 'v-green' : 'v-muted',
        'reserves redeemable fast', 'i-metric', tip.liquidReserves),
      mcell('NET REDEMPTION FLOW',
        flow != null ? fmtUSD(flow) : 'n/a',
        flow == null ? 'v-paper' : flow < 0 ? 'v-rose' : 'v-green',
        flow != null ? 'on-chain supply minus attested tokens outstanding'
                     : naFieldNote('no attestation baseline — hover'),
        'i-metric', flow != null ? tip.netRedemptionFlow : tip.na.drift),
      mcell('STALENESS', m && att ? m.staleness_days + ' days' : 'n/a',
        m && att && m.staleness_days > 35 ? 'v-amber' : 'v-paper',
        m && att ? 'age of the attestation'
                 : naFieldNote('no attestation date — hover'),
        'i-gate', m && att ? tip.staleness : tip.na.staleness),
    ));

    // Augmentation cards still render in normal path too (for crypto/
    // synthetic backing-model framing); they sit BELOW the deterministic
    // figures since those figures actually resolved.
    augCards.forEach((ctx) => snapBody.append(augmentationCard(ctx)));
  }

  if (supply.read_at) {
    snapBody.append(el('div', { class: 'prov-stamp' },
      el('span', { class: 'glyph' }, '§'),
      'on-chain reads taken at ' + supply.read_at));
  }
  mount.append(panel('01', 'REDEMPTION SNAPSHOT', 'i-facts', snapBody));

  // ── 02 RESERVE LIQUIDITY BREAKDOWN ──
  const tierBody = el('div', {});
  if (tiers.length) {
    const total = tiers.reduce((a, t) => a + Number(t.amount || 0), 0);
    const rows = tiers.map((t) => {
      const key = String(t.tier || '').toLowerCase().trim();
      const share = total ? Number(t.amount || 0) / total : null;
      return el('tr', { class: 'td-tier-' + (key || 'moderate') },
        el('td', {}, t.asset_class || '—'),
        el('td', {}, tierBadge(t.tier)),
        el('td', { class: 'num v-paper' }, fmtUSD(t.amount)),
        el('td', { class: 'num dim' }, share == null ? '—' : fmtPct(share)));
    });
    tierBody.append(el('table', { class: 'dtable' },
      el('thead', {}, el('tr', {},
        el('th', {}, 'ASSET CLASS'),
        el('th', { title: tip.tier }, 'TIER'),
        el('th', { class: 'num' }, 'AMOUNT'),
        el('th', { class: 'num' }, 'SHARE'))),
      el('tbody', {}, ...rows)));
    tierBody.append(el('div', { class: 'prov-note' },
      icon('i-info'),
      el('div', {},
        el('b', {}, 'Reserves tiered by redemption speed. '),
        'liquid funds redemptions immediately; moderate takes days; '
        + 'illiquid is slow to realise. Liquid coverage counts the liquid '
        + 'tier only.')));
  } else {
    // Empty-state copy is backing-model aware. For crypto / synthetic /
    // algorithmic tokens, fiat liquidity tiering doesn't apply by
    // design — point the reader to the protocol's live on-chain
    // dashboard instead of saying "we couldn't resolve". For fiat
    // tokens with a missing attestation, the message is honest but
    // also actionable (the AI Context above explains the gap).
    const bm = r.backing_model || 'fiat_reserves';
    const protoUrl = r.protocol_url || '';
    if (bm !== 'fiat_reserves') {
      const bmLabel = ({
        crypto_collateral: 'on-chain collateral',
        synthetic_delta_neutral: 'delta-neutral positions',
        algorithmic: 'algorithmic + partial-collateral mechanism',
        new_or_unverified: 'unverified backing model',
      })[bm] || 'on-chain backing';
      tierBody.append(el('div', { class: 'empty' },
        icon('i-metric'),
        el('b', {}, 'Fiat-style tier breakdown doesn\'t apply here'),
        el('div', {},
          r.symbol + ' is backed by ' + bmLabel + ' rather than ' +
          'off-chain reserves. Redemption is on-chain via the ' +
          'protocol\'s smart contracts. The live picture is on the ' +
          'protocol dashboard.',
          protoUrl ? el('div', { style: 'margin-top:10px' },
            el('a', { href: protoUrl, target: '_blank',
              rel: 'noopener', class: 'btn ghost' },
              icon('i-chain'), 'OPEN PROTOCOL DASHBOARD ↗')) : null)));
    } else {
      // Lead with what we DO know (issuer + cadence + where to look);
      // the raw-parsing limitation is a quiet footnote, not the headline.
      const issuer = r.issuer || r.symbol;
      const transparency = r.transparency_url || '';
      const body = el('div', {},
        `${issuer} publishes reserve composition in periodic CPA `
        + 'attestations. The AI Context above carries the qualitative '
        + 'picture (auditor, cadence, regulatory regime); the live PDF '
        + 'is the source of truth for the tier-by-tier liquidity split.');
      if (transparency) {
        body.append(el('div', { style: 'margin-top:10px' },
          el('a', { href: transparency, target: '_blank',
            rel: 'noopener', class: 'btn ghost' },
            icon('i-chain'), 'OPEN ISSUER TRANSPARENCY PAGE ↗')));
      }
      body.append(el('div', { class: 'soft-foot' },
        el('span', { class: 'glyph' }, '§'),
        'Automated tier extraction needs a machine-readable PDF; this '
        + 'run’s fetch returned no document, most often because the '
        + 'issuer page is JavaScript-rendered or the URL has rotated. '
        + 'Background discovery retries every six hours.'));
      tierBody.append(el('div', { class: 'empty' },
        icon('i-metric'),
        el('b', {}, 'Reserve composition lives in the issuer’s attestation'),
        body));
    }
  }
  // Panel title is backing-model aware: fiat tokens get the tier-count
  // label, non-fiat tokens get a model-appropriate title that signals
  // "this isn't a CPA-tiered breakdown by design". When the fiat path
  // has no tier rows yet, the title points at the issuer transparency
  // mechanism rather than apologising for the missing run.
  const panelTitle = tiers.length
    ? `RESERVE LIQUIDITY · ${tiers.length} TIER LINE(S)`
    : (r.backing_model || 'fiat_reserves') === 'fiat_reserves'
      ? 'RESERVE LIQUIDITY · per issuer transparency'
      : 'REDEMPTION MECHANISM · on-chain';
  mount.append(panel('02', panelTitle, 'i-metric', tierBody));

  // deployment provenance
  const chainTbl = supplyChainTable(supply);
  if (chainTbl) {
    mount.append(panel('03', 'PER-CHAIN SUPPLY BREAKDOWN', 'i-supply',
      chainTbl));
  }

  // ── GUARDRAIL CHECKS · REASONING FRAME · NARRATIVE · GAPS ──
  mount.append(checksPanel(chainTbl ? '04' : '03', r.checks));
  mount.append(passagesPanel(chainTbl ? '05' : '04', r.passages));
  mount.append(panel(chainTbl ? '06' : '05', 'REDEMPTION NARRATIVE', 'i-agent',
    r.narrative
      ? el('div', { class: 'narrative', html: markdown(r.narrative) })
      : el('div', { class: 'empty' }, icon('i-agent'),
          el('b', {}, 'No narrative synthesised.'))));
  mount.append(gapsPanel(chainTbl ? '07' : '06', r.gaps));

  mount.append(el('div', { style: 'padding:0 14px 24px;display:flex;gap:8px;flex-wrap:wrap' },
    el('button', { class: 'btn ghost',
      onclick: () => { location.hash = '#analyze/' + r.symbol; } },
      icon('i-agent'), 'ANALYZE ' + r.symbol),
    el('button', { class: 'btn ghost',
      onclick: () => { location.hash = '#sanctions/' + r.symbol; } },
      icon('i-sanction'), 'SANCTIONS ' + r.symbol)));
}

// ════════════════════════════════════════════════════════════════════
//  CORPUS VIEW
// ════════════════════════════════════════════════════════════════════
async function viewCorpus() {
  app.innerHTML = '';
  app.append(viewHead('F3', 'CORPUS',
    'the reasoning frame · every source is citable by default · a human can opt one out'));
  const mount = el('div', { class: 'view-body' });
  app.append(mount);
  mount.append(skeletonTable(5));

  let data;
  try { data = await api('/sources'); }
  catch (e) {
    mount.innerHTML = '';
    mount.append(errorBox('Could not load corpus', e.message, undefined,
      { onRetry: () => viewCorpus(), retryLabel: 'TRY AGAIN' }));
    return;
  }
  // share the freshly-loaded registry with the passage renderer (F2/F5/F6)
  // so it never has to re-fetch on its own.
  const map = {};
  (data.sources || []).forEach((s) => {
    if (!s || !s.id) return;
    map[s.id] = {
      title: s.title || '', url: s.url || '', tier: s.tier || '',
      verified: !!s.verified, included: s.included !== false,
    };
  });
  STATE.sources = map;
  STATE.sourcesLoaded = true;
  // Back-compat: pre-opt-out API exposed `approved` not `included`. If the
  // server hasn't restarted into the new shape, derive from `approved` so
  // the corpus dashboard renders instead of going NaN-empty.
  if (data.included == null && data.approved != null) {
    data.included = data.approved;
  }
  const excluded = data.count - (data.included || 0);
  logLine('OK', 'CORPUS', [
    seg('registry', 'lg-val'),
    seg('src' + data.count),
    seg('inc' + data.included, 'd-up'),
    seg('vfy' + data.verified),
    seg('exc' + excluded, excluded ? 'd-warn' : ''),
  ]);
  mount.innerHTML = '';

  mount.append(el('div', { class: 'strip fade-in' },
    stripCell(data.count, 'REGISTERED SOURCES', 'v-gold'),
    stripCell(data.included, 'INCLUDED · CITABLE', data.included ? 'v-green' : 'v-amber'),
    stripCell(data.verified, 'HUMAN-VERIFIED', 'v-gold'),
    stripCell(excluded, 'EXCLUDED · OPTED OUT', excluded ? 'v-amber' : '')));

  const list = el('div', { class: 'corpus-list' });
  data.sources.forEach((s) => list.append(corpusCard(s)));

  mount.append(panel(null, 'SOURCE REGISTRY · INCLUDED BY DEFAULT', 'i-frame', list));
}

// ── status → display config (opt-out model) ──────────────────────────
// included green (default) · excluded rose.
function sourceStatus(s) {
  const raw = String(s.status || (s.included === false ? 'excluded' : 'included'))
    .toLowerCase().trim();
  if (raw === 'excluded') return { key: 'excluded', label: 'EXCLUDED', cls: 'alert' };
  return { key: 'included', label: 'INCLUDED', cls: 'ok' };
}

// one source card — title · tier · status · summary · link · toggle controls
function corpusCard(s) {
  const st = sourceStatus(s);
  const card = el('div', { class: 'corpus-card src-' + st.key, 'data-id': s.id });

  const tags = el('div', { class: 'corpus-tags' },
    el('span', { class: 'stag ' + st.cls }, st.label));
  if (s.verified)
    tags.append(el('span', { class: 'stag ok has-tip',
      title: 'A human has explicitly reviewed this source.' }, '✦ VERIFIED'));

  card.append(el('div', { class: 'corpus-top' },
    el('div', { class: 'corpus-id' },
      el('span', { class: 'corpus-title' }, s.title || s.id),
      el('span', { class: 'corpus-tier' }, (s.tier || 'untiered').toUpperCase())),
    tags));

  if (s.summary)
    card.append(el('div', { class: 'corpus-summary' }, s.summary));
  if (s.notes)
    card.append(el('div', { class: 'corpus-notes' },
      el('span', { class: 'corpus-notes-k' }, 'NOTE'), s.notes));

  const foot = el('div', { class: 'corpus-foot' });
  foot.append(s.url
    ? el('a', { class: 'cite corpus-link',
        href: s.url, target: '_blank', rel: 'noopener noreferrer' },
        'view source ', el('span', { class: 'glyph' }, '↗'))
    : el('span', { class: 'dim' }, 'no source url'));

  // Opt-out model: a toggle (exclude / re-include) plus a verify lever.
  const toggleBtn = s.included === false
    ? el('button', { class: 'btn vote-btn' }, 'INCLUDE')
    : el('button', { class: 'btn ghost vote-btn' }, 'EXCLUDE');
  const verifyBtn = el('button', { class: 'btn ghost vote-btn' },
    s.verified ? 'VERIFIED ✦' : 'MARK VERIFIED');
  if (s.verified) verifyBtn.disabled = true;
  const btns = [toggleBtn, verifyBtn];

  const vote = async (decision, btn) => {
    // Curation is a save-type action — gated on an account. Anonymous
    // callers see a friendly invitation instead of firing a doomed 401.
    if (!AUTH.signedIn()) {
      AUTH.openPanel('in', 'include, exclude or verify corpus sources');
      logLine('WORK', 'CORPUS', [seg(s.id, 'lg-sym'),
        seg('sign-in required to record a curation decision')]);
      return;
    }
    const labels = btns.map((b) => b.textContent);
    btns.forEach((b) => { b.disabled = true; });
    btn.replaceChildren(el('span', { class: 'spinner' }));
    try {
      const res = await api('/sources/' + encodeURIComponent(s.id) + '/vote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      });
      logLine('OK', 'CORPUS', [
        seg(s.id, 'lg-sym'),
        seg(decision, decision === 'excluded' ? 'd-warn' : 'd-up'),
        seg(res.ingested ? 'staged text ingested · citeable'
          : res.ingest_error ? 'ingest failed: ' + res.ingest_error
          : 'decision recorded'),
      ]);
      // refresh this card to its new state
      const next = res.source || Object.assign({}, s, {
        status: decision === 'excluded' ? 'excluded' : 'included',
        included: decision !== 'excluded',
        verified: decision === 'verified' || s.verified,
      });
      card.replaceWith(corpusCard(next));
    } catch (e) {
      btns.forEach((b, i) => {
        b.disabled = false;
        b.replaceChildren(document.createTextNode(labels[i]));
      });
      if (e.status === 401) {
        // session lapsed mid-action — re-invite rather than alarm
        AUTH.openPanel('in', 'include, exclude or verify corpus sources');
        logLine('WORK', 'CORPUS', [seg(s.id, 'lg-sym'),
          seg('session expired · sign in again to save the decision')]);
        return;
      }
      logLine('ERR', 'CORPUS', [
        seg(s.id, 'lg-sym'), seg('VOTE-FAIL', 'd-warn'),
        seg(String(e.message).slice(0, 48)),
      ]);
    }
  };
  toggleBtn.addEventListener('click', () =>
    vote(s.included === false ? 'included' : 'excluded', toggleBtn));
  verifyBtn.addEventListener('click', () => vote('verified', verifyBtn));
  foot.append(el('div', { class: 'corpus-votes' }, toggleBtn, verifyBtn));

  card.append(foot);
  return card;
}

// ════════════════════════════════════════════════════════════════════
//  EVALS VIEW
// ════════════════════════════════════════════════════════════════════
function viewEvals() {
  app.innerHTML = '';
  const runBtn = el('button', { class: 'btn' }, icon('i-eval'), 'RUN SUITE');
  app.append(viewHead('F4', 'EVALS',
    'regression guard · one live analysis per case — slow', runBtn));
  const mount = el('div', { class: 'view-body' });
  app.append(mount);
  // Mirror analyze/sanctions/redemption: try the cached endpoint
  // first so a returning user sees the last suite instantly with a
  // freshness strip. Only a true cache miss shows the idle empty
  // state. RUN SUITE always forces a fresh POST.
  runBtn.addEventListener('click', () => runEvalSuite(runBtn, mount));
  loadCachedEvals(runBtn, mount);
}

async function loadCachedEvals(runBtn, mount) {
  mount.innerHTML = '';
  mount.append(el('div', { class: 'empty' },
    icon('i-eval'),
    el('b', {}, 'Loading last eval suite…')));
  let payload;
  try { payload = await api('/evals'); }
  catch { payload = { cached: false }; }
  if (!payload || !payload.cached) {
    mount.innerHTML = '';
    mount.append(el('div', { class: 'empty' },
      icon('i-eval'),
      el('b', {}, 'Eval suite idle'),
      el('div', {}, 'Each case runs a full analysis and grades structured ' +
        'expectations. Press RUN SUITE — this takes a while.')));
    runBtn.replaceChildren(icon('i-eval'), document.createTextNode('RUN SUITE'));
    return;
  }
  mount.innerHTML = '';
  // Surface the same freshness strip every other view uses so the user
  // sees "computed 12m ago" + a contextual RE-RUN that matches the
  // analyze / sanctions / redemption pattern exactly.
  mount.append(freshnessStrip(payload.computed_at,
    () => runEvalSuite(runBtn, mount)));
  logLine('OK', 'EVALS', [
    seg(payload.passed === payload.count ? 'GREEN' : 'FAIL',
      payload.passed === payload.count ? 'd-up' : 'd-dn'),
    seg(payload.stale ? 'stale cache' : 'fresh cache',
      payload.stale ? 'd-warn' : 'd-up'),
    seg('pass' + payload.passed + '/' + payload.count),
  ]);
  renderEvals(payload, mount);
  runBtn.replaceChildren(icon('i-eval'), document.createTextNode('RE-RUN'));
}

async function runEvalSuite(runBtn, mount) {
  runBtn.disabled = true;
  runBtn.replaceChildren(el('span', { class: 'spinner' }),
    document.createTextNode(' RUNNING'));
  mount.innerHTML = '';
  logLine('WORK', 'EVALS', [seg('suite start', 'lg-val'),
    seg('grading all cases')]);
  // Dynamic narration during the synchronous eval run — same shape
  // as the analyze view's stage panel including the latency hint
  // (typical-time line + clock/scanbar) so the user knows how long
  // to wait. evals uses one cross-suite latency bucket.
  const expected = expectedLatency('evals', 'suite');
  const subLine = el('div', { class: 'rs-sub rs-sub-tick' },
    'loading the eval case list');
  const clock = el('span', { class: 'rs-tk',
    style: 'margin-left:auto' }, '0s / ~' + expected.seconds + 's');
  const head = el('div', { class: 'rstage active' },
    el('div', { class: 'rs-mark' }),
    el('div', { class: 'rs-text' },
      el('div', { class: 'rs-label' }, 'Eval suite running'),
      subLine),
    clock);
  const scanbar = el('div', { class: 'scanbar' });
  const waitHint = el('div', { class: 'rs-wait' },
    el('span', { class: 'rs-wait-mark' }, '◇'),
    el('span', { class: 'rs-wait-txt' },
      latencyHint('evals', 'suite')));
  mount.append(panel(null, 'EVAL SUITE — RUNNING', 'i-eval',
    el('div', {},
      el('div', { class: 'run-stages' }, head),
      scanbar,
      waitHint)));
  const evalLines = [
    'loading the eval case list',
    'running USDC supply-resolves check',
    'running USDT supply-resolves check',
    'running PYUSD attestation-absent expectation',
    'verifying GUSD gap-contains assertions',
    'checking the corpus-included-by-default guard',
    'asserting no investment-advice language in narratives',
    'grading attestation-confidence thresholds',
    'tallying pass / fail across every case',
  ];
  let i = 0;
  const t0 = Date.now();
  const subTimer = setInterval(() => {
    i = (i + 1) % evalLines.length;
    subLine.textContent = evalLines[i];
    subLine.classList.remove('rs-sub-tick');
    void subLine.offsetWidth;
    subLine.classList.add('rs-sub-tick');
  }, 1600);
  const clockTimer = setInterval(() => {
    const elapsedS = Math.round((Date.now() - t0) / 1000);
    const over = elapsedS > expected.seconds * 1.5;
    const wayOver = elapsedS > expected.seconds * 2.5;
    clock.textContent = over ? elapsedS + 's'
      : elapsedS + 's / ~' + expected.seconds + 's';
    clock.className = 'rs-tk' + (wayOver ? ' rs-tk-slow'
      : over ? ' rs-tk-overrun' : '');
    if (wayOver) {
      waitHint.classList.add('rs-wait-slow');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'Taking longer than usual. Still working — every case includes a '
        + 'live analyze() with its own LLM hop.';
    } else if (over) {
      waitHint.classList.add('rs-wait-over');
      waitHint.querySelector('.rs-wait-txt').textContent =
        'A little past the typical ' + expected.seconds + 's, still on track.';
    }
    const pct = Math.min(96, Math.round((elapsedS / expected.seconds) * 100));
    scanbar.style.setProperty('--scan-pct', pct + '%');
  }, 250);
  let data;
  try {
    data = await api('/evals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
  } catch (e) {
    clearInterval(subTimer); clearInterval(clockTimer);
    mount.innerHTML = '';
    logLine('ERR', 'EVALS', [seg('SUITE-FAIL', 'd-warn'),
      seg(String(e.message).slice(0, 48))]);
    mount.append(errorBox('Eval run failed', e.message, undefined,
      { onRetry: () => runEvalSuite(runBtn, mount),
        retryLabel: 'TRY AGAIN' }));
    runBtn.disabled = false;
    runBtn.replaceChildren(icon('i-eval'),
      document.createTextNode('RUN SUITE'));
    return;
  }
  clearInterval(subTimer); clearInterval(clockTimer);
  if (data.elapsed_s) recordLatency('evals', 'suite', data.elapsed_s);
  mount.innerHTML = '';
  mount.append(freshnessStrip(data.computed_at,
    () => runEvalSuite(runBtn, mount)));
  logLine(data.passed === data.count ? 'OK' : 'ALERT', 'EVALS', [
    seg(data.passed === data.count ? 'GREEN' : 'FAIL',
      data.passed === data.count ? 'd-up' : 'd-dn'),
    seg('pass' + data.passed + '/' + data.count),
    seg('fail' + (data.count - data.passed),
      data.count - data.passed ? 'd-warn' : ''),
  ]);
  renderEvals(data, mount);
  runBtn.disabled = false;
  runBtn.replaceChildren(icon('i-eval'), document.createTextNode('RE-RUN'));
}

// Eval points come back as programmatic kind strings ("supply_resolved",
// "gap_contains(identity check)", "passages_included_only") with raw
// assertion details ("total_supply=72,503,244,574.51", "needle='X'",
// "excluded_cited=[]"). That reads as test-runner output, not as a
// product. This humaniser turns each into a sentence a financial
// reader can scan. The raw kind stays available on hover for the
// audit-minded.
function humanizeEvalPoint(p) {
  const raw = p.point || '';
  const det = p.detail || '';
  // Split "gap_contains(some phrase)" into kind + needle.
  const m = raw.match(/^([a-z_]+)(?:\((.*)\))?$/);
  const kind = m ? m[1] : raw;
  const arg = m && m[2] ? m[2] : '';

  // Pretty assertion-detail: "total_supply=72,503..." → "Total supply: 72,503..."
  const prettyDetail = () => {
    if (!det) return '';
    const kv = det.match(/^([a-z_]+)=(.+)$/);
    if (kv) {
      const k = kv[1].replace(/_/g, ' ');
      const v = kv[2].replace(/^['"]|['"]$/g, '');
      return k.charAt(0).toUpperCase() + k.slice(1) + ': ' + v;
    }
    return det;
  };

  const labels = {
    supply_resolved: {
      ok: 'Live supply resolved from on-chain reads',
      fail: 'Live supply did not resolve',
    },
    metrics_present: {
      ok: 'Coverage metrics computed',
      fail: 'Coverage metrics missing',
    },
    metrics_absent: {
      ok: 'Coverage metrics correctly absent for this case',
      fail: 'Coverage metrics were computed when none were expected',
    },
    narrative_present: {
      ok: 'Narrative synthesised',
      fail: 'No narrative was synthesised',
    },
    no_investment_language: {
      ok: 'Narrative is free of investment-advice language',
      fail: 'Narrative contains banned investment-advice phrasing',
    },
    passages_included_only: {
      ok: 'Reasoning frame cites only included corpus passages',
      fail: 'Reasoning frame cited an excluded passage',
    },
    attestation_confidence_min: {
      ok: 'Attestation extraction confidence cleared the threshold',
      fail: 'Attestation extraction confidence fell below the threshold',
    },
    gap_contains: {
      ok: arg
        ? `Expected gap mentioning "${arg}" was reported`
        : 'Expected gap was reported',
      fail: arg
        ? `Expected gap mentioning "${arg}" was missing`
        : 'Expected gap was missing',
    },
  };
  const entry = labels[kind];
  const name = entry
    ? (p.passed ? entry.ok : entry.fail)
    : raw;
  return { name, detail: prettyDetail(), rawKind: raw };
}

function renderEvals(data, mount) {
  const allPass = data.passed === data.count;
  mount.append(el('div', { class: 'strip fade-in' },
    stripCell(data.count, 'CASES'),
    stripCell(data.passed, 'PASSED', data.passed ? 'v-green' : 'v-rose'),
    stripCell(data.count - data.passed, 'FAILED',
      data.count - data.passed ? 'v-rose' : 'v-green'),
    stripCell(allPass ? 'GREEN' : 'FAIL', 'SUITE', allPass ? 'v-green' : 'v-rose')));

  data.cases.forEach((c) => {
    const body = el('div', {}, ...c.points.map((p) => {
      const h = humanizeEvalPoint(p);
      return el('div', {
        class: 'check' + (p.passed ? '' : ' fail-critical'),
        title: 'check id: ' + h.rawKind,
      },
        icon(p.passed ? 'i-ok' : 'i-error'),
        el('div', {},
          el('div', { class: 'check-name' }, h.name),
          h.detail ? el('div', { class: 'check-detail' }, h.detail) : null),
        el('span', { class: 'sev ' + (p.passed ? 'pass' : 'sev-critical') },
          p.passed ? 'pass' : 'fail'));
    }));
    mount.append(panel(null, `${c.case_id} · ${c.symbol}`, c.passed ? 'i-ok' : 'i-error', body));
  });
}

// ── shared loaders ───────────────────────────────────────────────────
function skeletonTable(rows) {
  const box = el('div', { class: 'pad' });
  for (let i = 0; i < rows; i++) {
    box.append(el('div', { class: 'skln', style: `width:${40 + (i * 13) % 50}%` }));
  }
  return box;
}

// ════════════════════════════════════════════════════════════════════
//  F7 · ANALYST — the Doré conversational console
//  A terminal-native chat with Doré, the analyst built on the Hermes
//  runtime. The user asks compliance questions in plain language; Doré
//  answers, citing its work. Transparency is the aesthetic: every
//  verification Doré runs is rendered as an inline log line inside its
//  reply, styled like the operations feed — seeing the work is the trust.
//
//  The Hermes runtime deploys separately. Until then the bridge returns
//  `runtime_offline` and the console shows a calm, designed panel — the
//  whole view stays beautifully rendered.
// ════════════════════════════════════════════════════════════════════
const ANALYST = {
  turns: [],          // { role:'user'|'dore', text, trace, status }
  pending: null,      // a question queued from the command line
  busy: false,
};

// the example questions — a preview of what Doré does. Click to ask.
const ANALYST_EXAMPLES = [
  'Is USDC’s attestation current and fully backed?',
  'Compare USDT and USDC on redemption strength',
  'What would an examiner ask about PYUSD?',
];

// Doré often narrates its verifications as lines that begin with an arrow
// ("→ ran attestation analysis · USDC"). We lift those out of the
// reply body and render them as the operations-log-style trace, so the
// answer reads clean and the work reads as a transparent record.
function splitAnalystReply(reply) {
  const trace = [];
  const body = [];
  for (const ln of String(reply || '').split('\n')) {
    const m = ln.match(/^\s*(?:[→>‣▸]|->)\s+(.*\S)\s*$/);
    if (m) trace.push(m[1]);
    else body.push(ln);
  }
  return { trace, body: body.join('\n').trim() };
}

// classify a trace line so it gets the right glyph — a verification run,
// a corpus search, or a generic step.
function analystTraceIcon(line) {
  const s = line.toLowerCase();
  if (/corpus|regulation|frame|search/.test(s)) return 'i-frame';
  if (/sanction|ofac|screen/.test(s)) return 'i-sanction';
  if (/redempt/.test(s)) return 'i-redeem';
  if (/supply|on-chain|chain/.test(s)) return 'i-supply';
  if (/attestation|reserve|analy/.test(s)) return 'i-doc';
  if (/histor|past/.test(s)) return 'i-metric';
  return 'i-agent';
}

// one operations-log-style trace line inside a Doré response
function analystTraceLine(line) {
  return el('div', { class: 'an-trace-line' },
    icon(analystTraceIcon(line), 'an-trace-ic'),
    el('span', { class: 'an-trace-arrow' }, '→'),
    el('span', { class: 'an-trace-text' }, line));
}

// the trace block — the visible record of what Doré did this turn
function analystTraceBlock(trace) {
  if (!trace || !trace.length) return null;
  return el('div', { class: 'an-trace' },
    el('div', { class: 'an-trace-head' },
      el('span', { class: 'an-trace-dot' }),
      el('span', {}, 'VERIFICATIONS RUN · ' + trace.length)),
    el('div', { class: 'an-trace-body' }, ...trace.map(analystTraceLine)));
}

// render a single transcript turn into the scroll
function renderAnalystTurn(turn) {
  if (turn.role === 'user') {
    return el('div', { class: 'an-turn an-turn-user fade-in' },
      el('div', { class: 'an-avatar an-avatar-user' }, 'YOU'),
      el('div', { class: 'an-bubble an-bubble-user' },
        el('div', { class: 'an-msg' }, turn.text)));
  }
  // Doré's turn
  const card = el('div', { class: 'an-turn an-turn-dore fade-in' });
  const avatar = el('div', { class: 'an-avatar an-avatar-dore' },
    icon('i-analyst', 'an-avatar-ic'));
  const bubble = el('div', { class: 'an-bubble an-bubble-dore' });

  if (turn.status === 'thinking') {
    bubble.append(el('div', { class: 'an-thinking' },
      el('span', { class: 'an-think-dot' }),
      el('span', { class: 'an-think-dot' }),
      el('span', { class: 'an-think-dot' }),
      el('span', { class: 'an-think-label' }, 'Doré is verifying…')));
    card.append(avatar, bubble);
    return card;
  }

  if (turn.status === 'offline') {
    bubble.append(analystOfflinePanel());
    card.append(avatar, bubble);
    return card;
  }

  if (turn.status === 'error') {
    bubble.append(el('div', { class: 'an-error' },
      icon('i-warn', 'an-err-ic'),
      el('div', {},
        el('b', {}, 'The analyst could not complete this turn.'),
        el('div', { class: 'an-err-detail' }, turn.text))));
    card.append(avatar, bubble);
    return card;
  }

  // a normal answer — trace first (the work), then the cited prose
  const block = analystTraceBlock(turn.trace);
  if (block) bubble.append(block);
  const answer = el('div', { class: 'an-answer narrative' });
  answer.innerHTML = markdown(turn.text || '');
  bubble.append(answer);
  card.append(avatar, bubble);
  return card;
}

// the designed offline state — calm, not an error. Shown when the Hermes
// runtime is not yet installed (the expected pre-deployment condition).
function analystOfflinePanel() {
  const panel = el('div', { class: 'an-offline' });
  panel.append(
    el('div', { class: 'an-offline-mark' }, icon('i-analyst', 'an-off-ic')),
    el('div', { class: 'an-offline-tag' }, 'ANALYST RUNTIME · STANDING BY'),
    el('div', { class: 'an-offline-title' },
      'Doré’s analyst activates on deployment'),
    el('p', { class: 'an-offline-body' },
      'The conversational analyst runs on the Hermes runtime, which is '
      + 'provisioned as its own deployment artefact. It is not yet on this '
      + 'environment — so the console is in preview. Everything you see '
      + 'is live; only the analyst’s reasoning loop awaits its runtime.'),
    el('div', { class: 'an-offline-rule' }),
    el('div', { class: 'an-offline-sub' },
      'WHEN LIVE, DORÉ WILL'));
  const cap = el('div', { class: 'an-offline-caps' });
  [['i-supply', 'Read live on-chain supply across every deployment'],
   ['i-doc', 'Run the full attestation analysis against claimed reserves'],
   ['i-sanction', 'Screen a token against the OFAC sanctions list'],
   ['i-frame', 'Ground every judgement in the curated corpus, and cite it'],
  ].forEach(([ic, txt]) => {
    cap.append(el('div', { class: 'an-cap' },
      icon(ic, 'an-cap-ic'), el('span', {}, txt)));
  });
  panel.append(cap);
  return panel;
}

// the empty-console state — Doré's identity + clickable example questions
function analystWelcome() {
  const wrap = el('div', { class: 'an-welcome fade-in' });
  wrap.append(
    el('div', { class: 'an-hero-mark' }, icon('i-analyst', 'an-hero-ic')),
    el('div', { class: 'an-hero-name' },
      'Dor', el('span', { class: 'acc' }, 'é')),
    el('div', { class: 'an-hero-role' }, 'COMPLIANCE ANALYST'),
    el('p', { class: 'an-hero-blurb' },
      'A stablecoin issuer claims its tokens are fully backed. That claim '
      + 'is a doré bar — real value, unverified. Doré is the '
      + 'assay: it reconciles what issuers attest against what the chain '
      + 'actually shows, and cites every step. Ask in plain language.'));
  const ex = el('div', { class: 'an-examples' });
  ex.append(el('div', { class: 'an-examples-head' }, 'TRY ASKING'));
  ANALYST_EXAMPLES.forEach((q) => {
    ex.append(el('button', { class: 'an-example', type: 'button',
      onclick: () => sendAnalyst(q) },
      el('span', { class: 'an-ex-q' }, '?'),
      el('span', {}, q)));
  });
  wrap.append(ex);
  return wrap;
}

// render the whole transcript into the scroll region
function renderAnalystScroll() {
  const scroll = $('#an-scroll');
  if (!scroll) return;
  scroll.innerHTML = '';
  if (!ANALYST.turns.length) {
    scroll.append(analystWelcome());
    return;
  }
  ANALYST.turns.forEach((t) => scroll.append(renderAnalystTurn(t)));
  scroll.scrollTop = scroll.scrollHeight;
}

// send one message to Doré through the /api/agent bridge
async function sendAnalyst(message) {
  message = String(message || '').trim();
  if (!message || ANALYST.busy) return;
  ANALYST.busy = true;
  ANALYST.turns.push({ role: 'user', text: message });
  const thinking = { role: 'dore', status: 'thinking', text: '' };
  ANALYST.turns.push(thinking);
  renderAnalystScroll();
  setAnalystComposerBusy(true);
  logLine('WORK', 'ANALYST', [seg('query sent', 'lg-val'),
    seg(message.slice(0, 44))]);

  try {
    const res = await api('/agent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    const idx = ANALYST.turns.indexOf(thinking);
    if (res.status === 'runtime_offline') {
      ANALYST.turns[idx] = { role: 'dore', status: 'offline', text: '' };
      logLine('WATCH', 'ANALYST', [seg('runtime offline', 'd-warn'),
        seg('Hermes activates on deployment')]);
    } else {
      const { trace, body } = splitAnalystReply(res.reply);
      const fullTrace = (res.trace && res.trace.length)
        ? res.trace.concat(trace) : trace;
      ANALYST.turns[idx] = { role: 'dore', status: 'ok',
        text: body || res.reply || '', trace: fullTrace };
      logLine('OK', 'ANALYST', [seg('reply received', 'lg-val'),
        seg('verifications ' + fullTrace.length)]);
    }
  } catch (e) {
    const idx = ANALYST.turns.indexOf(thinking);
    ANALYST.turns[idx] = { role: 'dore', status: 'error',
      text: String(e.message || 'request failed') };
    logLine('ERR', 'ANALYST', [seg('QUERY-FAIL', 'd-warn'),
      seg(String(e.message).slice(0, 44))]);
  } finally {
    ANALYST.busy = false;
    setAnalystComposerBusy(false);
    renderAnalystScroll();
  }
}

function setAnalystComposerBusy(busy) {
  const inp = $('#an-input');
  const btn = $('#an-send');
  if (inp) inp.disabled = busy;
  if (btn) {
    btn.disabled = busy;
    btn.replaceChildren(busy
      ? el('span', { class: 'spinner' })
      : icon('i-agent', 'an-send-ic'),
      document.createTextNode(busy ? ' VERIFYING' : ' ASK'));
  }
  if (!busy && inp) inp.focus();
}

function viewAnalyst() {
  app.innerHTML = '';
  app.append(viewHead('F7', 'ANALYST',
    'Doré — conversational compliance analyst · cites its work'));

  const console = el('div', { class: 'an-console fade-in' });

  // a slim header strip identifying the analyst
  console.append(el('div', { class: 'an-bar' },
    el('span', { class: 'an-bar-mark' }, icon('i-analyst', 'an-bar-ic')),
    el('span', { class: 'an-bar-name' },
      'DOR', el('span', { class: 'acc' }, 'É')),
    el('span', { class: 'an-bar-sep' }, '│'),
    el('span', { class: 'an-bar-desc' },
      'reasons in plain language · verifies through read-only checks '
      + '· never invents a figure'),
    el('span', { class: 'an-bar-runtime' },
      el('span', { class: 'an-bar-dot' }), 'HERMES RUNTIME')));

  // the transcript scroll
  console.append(el('div', { class: 'an-scroll', id: 'an-scroll' }));

  // the composer — consistent with the bottom command-line styling
  const input = el('input', { class: 'an-input', id: 'an-input',
    type: 'text', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'Ask Doré a compliance question…' });
  const send = el('button', { class: 'an-send', id: 'an-send', type: 'button' },
    icon('i-agent', 'an-send-ic'), document.createTextNode(' ASK'));
  const submit = () => {
    const v = input.value.trim();
    if (!v) return;
    input.value = '';
    sendAnalyst(v);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
  send.addEventListener('click', submit);
  console.append(el('div', { class: 'an-composer' },
    el('span', { class: 'an-prompt' }, 'ASK DORÉ›'),
    input, send));

  app.append(console);
  renderAnalystScroll();

  // a question queued from the command line (`ask <question>`) fires now
  if (ANALYST.pending) {
    const q = ANALYST.pending;
    ANALYST.pending = null;
    sendAnalyst(q);
  } else {
    setTimeout(() => { const i = $('#an-input'); if (i) i.focus(); }, 30);
  }
}

// ════════════════════════════════════════════════════════════════════
//  COMMAND LINE
// ════════════════════════════════════════════════════════════════════
function runCommand(raw) {
  const parts = raw.trim().split(/\s+/);
  const cmd = (parts[0] || '').toLowerCase();
  const arg = (parts[1] || '').toUpperCase();
  if (!cmd) return;
  if (cmd === 'clear' || cmd === 'cls') {
    STATE.feed = []; STATE.feedSeq = 0; _feedRendered = 0;
    const f = $('#op-feed'); if (f) f.innerHTML = '';
    const ln = $('#ts-lines'); if (ln) ln.textContent = '0';
    logLine('INFO', 'CONSOLE', [seg('feed cleared', 'lg-val'), seg('by operator')]);
    return;
  }
  if (cmd === 'monitor' || cmd === 'mon') { location.hash = '#monitor'; return; }
  if (cmd === 'corpus') { location.hash = '#corpus'; return; }
  if (cmd === 'evals' || cmd === 'eval') { location.hash = '#evals'; return; }
  if (cmd === 'screen' || cmd === 'sanctions' || cmd === 'sanction') {
    location.hash = '#sanctions' + (arg ? '/' + arg : '');
    return;
  }
  if (cmd === 'redeem' || cmd === 'redemption' || cmd === 'redemptions') {
    location.hash = '#redemptions' + (arg ? '/' + arg : '');
    return;
  }
  if (cmd === 'analyst' || cmd === 'ask' || cmd === 'dore' || cmd === 'doré') {
    location.hash = '#analyst';
    // a bare `ask <question…>` jumps to the console and sends it directly
    const rest = raw.trim().replace(/^\S+\s*/, '').trim();
    if (rest) ANALYST.pending = rest;
    return;
  }
  if (cmd === 'refresh' || cmd === 'force') {
    if (arg) { location.hash = '#analyze/' + arg; }
    logLine('WORK', 'CONSOLE', [seg('force refresh', 'd-warn'),
      seg(arg || 'use ANALYZE view')]);
    return;
  }
  if (cmd === 'analyze' || cmd === 'an' || cmd === 'a') {
    if (arg) location.hash = '#analyze/' + arg;
    else location.hash = '#analyze';
    return;
  }
  // bare symbol → analyze it
  if (STATE.tokens.some((t) => t.symbol === cmd.toUpperCase())) {
    location.hash = '#analyze/' + cmd.toUpperCase();
    return;
  }
  logLine('WATCH', 'CONSOLE', [seg('unknown cmd', 'd-warn'),
    seg(raw.trim().slice(0, 48))]);
}

// ════════════════════════════════════════════════════════════════════
//  F8 · COMPENDIUM — the live freshness & background-thread page
// ════════════════════════════════════════════════════════════════════
// One observable view onto how often each artefact is refreshed: per-
// token attestation URL provenance, per-source corpus health, web-
// discovery activity, and a rolling event tail. Data IS the moat, so
// expose how recent and verified it is at a glance.
let compendiumTimer = null;

function viewCompendium() {
  app.innerHTML = '';
  if (compendiumTimer) { clearInterval(compendiumTimer); compendiumTimer = null; }
  // Manual refresh affordance — same canonical control pattern as the
  // analyze surface. The page auto-refreshes every 30s; clicking
  // REFRESH triggers an immediate re-fetch (the underlying endpoint is
  // a fast aggregator over local state, no quota burn).
  const refreshBtn = el('button', { class: 'btn ghost',
    title: 'Re-fetch the compendium snapshot now (auto-refreshes every 30s)',
  }, icon('i-supply'), 'REFRESH');
  app.append(viewHead('F8', 'COMPENDIUM',
    'live ledger of the data layer · auto-refreshes', refreshBtn));
  const mount = el('div', { class: 'view-body' });
  app.append(mount);
  mount.append(el('div', { class: 'empty' },
    el('span', { class: 'spinner' }),
    el('b', {}, 'Loading compendium…')));

  const tick = async () => {
    let data;
    try { data = await api('/compendium'); }
    catch (e) {
      mount.innerHTML = '';
      mount.append(errorBox('Compendium load failed', e.message,
        undefined, { onRetry: tick, retryLabel: 'TRY AGAIN' }));
      return;
    }
    renderCompendium(data, mount);
  };
  tick();
  compendiumTimer = setInterval(tick, 30_000);
  refreshBtn.addEventListener('click', () => {
    refreshBtn.disabled = true;
    refreshBtn.replaceChildren(el('span', { class: 'spinner' }),
      document.createTextNode(' refreshing'));
    tick().finally(() => {
      refreshBtn.disabled = false;
      refreshBtn.replaceChildren(icon('i-supply'),
        document.createTextNode('REFRESH'));
    });
  });
  logLine('WATCH', 'COMPENDIUM', [seg('engaged', 'lg-val'),
    seg('30s auto-refresh')]);
}

function _fmtTs(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    const day = d.toISOString().slice(0, 10);
    const time = d.toISOString().slice(11, 16);
    return day + ' ' + time + ' UTC';
  } catch (_) { return ts; }
}

function _daysSince(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return Math.floor((Date.now() - d.getTime()) / 86_400_000);
}

function buildFlowSection() {
  // Inline SVG flow diagram — narrates how a question becomes a
  // verified answer. Five stations, gold accents on the gates that
  // matter (HEAD check + content_hash trust gate). Renders at any
  // viewport because viewBox + preserveAspectRatio.
  const svg = `
  <svg viewBox="0 0 900 230" xmlns="http://www.w3.org/2000/svg"
       role="img" aria-label="How data flows from search to verified fact"
       class="cp-flow-svg">
    <!-- background hairline grid -->
    <defs>
      <marker id="arr" viewBox="0 0 10 10" refX="8" refY="5"
              markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#D4A24A"/>
      </marker>
    </defs>
    <!-- Stations -->
    <g font-family="'IBM Plex Mono', monospace" font-size="10"
       letter-spacing="1.2" fill="#c9b88f">
      <rect x="20"  y="40" width="140" height="80" fill="rgba(212,162,74,.06)"
            stroke="rgba(212,162,74,.4)"/>
      <text x="90" y="68" text-anchor="middle" fill="#d4a24a">SEARCH</text>
      <text x="90" y="86" text-anchor="middle">Brave + DDG</text>
      <text x="90" y="104" text-anchor="middle" font-size="9">authoritative-domain rank</text>

      <rect x="200" y="40" width="140" height="80" fill="rgba(212,162,74,.06)"
            stroke="rgba(212,162,74,.4)"/>
      <text x="270" y="68" text-anchor="middle" fill="#d4a24a">VALIDATE</text>
      <text x="270" y="86" text-anchor="middle">HEAD-check PDF</text>
      <text x="270" y="104" text-anchor="middle" font-size="9">+ schema-grounded extract</text>

      <rect x="380" y="40" width="140" height="80" fill="rgba(212,162,74,.06)"
            stroke="rgba(212,162,74,.4)"/>
      <text x="450" y="68" text-anchor="middle" fill="#d4a24a">GUARDRAILS</text>
      <text x="450" y="86" text-anchor="middle">10 checks</text>
      <text x="450" y="104" text-anchor="middle" font-size="9">coverage / sums / dates</text>

      <rect x="560" y="40" width="140" height="80" fill="rgba(212,162,74,.10)"
            stroke="rgba(212,162,74,.6)" stroke-width="1.5"/>
      <text x="630" y="68" text-anchor="middle" fill="#d4a24a">FACT STORE</text>
      <text x="630" y="86" text-anchor="middle">append-only</text>
      <text x="630" y="104" text-anchor="middle" font-size="9">sha256 content-hash</text>

      <rect x="740" y="40" width="140" height="80" fill="rgba(127,224,166,.08)"
            stroke="rgba(127,224,166,.6)"/>
      <text x="810" y="68" text-anchor="middle" fill="#7FE0A6">USER VIEW</text>
      <text x="810" y="86" text-anchor="middle">cited + tiered</text>
      <text x="810" y="104" text-anchor="middle" font-size="9">never bare n/a</text>
    </g>
    <!-- Arrows -->
    <g stroke="#D4A24A" stroke-width="1.4" fill="none" marker-end="url(#arr)">
      <line x1="160" y1="80" x2="195" y2="80"/>
      <line x1="340" y1="80" x2="375" y2="80"/>
      <line x1="520" y1="80" x2="555" y2="80"/>
      <line x1="700" y1="80" x2="735" y2="80"/>
    </g>
    <!-- Below the line: background canary loop -->
    <g font-family="'IBM Plex Mono', monospace" font-size="9"
       letter-spacing="1" fill="#8b7d62">
      <line x1="90" y1="160" x2="810" y2="160"
            stroke="rgba(212,162,74,.18)" stroke-dasharray="3,3"/>
      <text x="450" y="184" text-anchor="middle" font-size="10"
            fill="#d4a24a" letter-spacing="2">BACKGROUND CANARY · EVERY 6 HOURS</text>
      <text x="450" y="200" text-anchor="middle">
        re-resolves URLs · re-checks corpus · auto-verifies contracts ·
        proposes replacements for broken sources
      </text>
    </g>
  </svg>`;
  const wrap = el('section', { class: 'cp-doc-section', id: 'flow' },
    el('h2', {}, 'How the data flows'),
    el('p', { class: 'cp-doc-para' },
      'A question lands on Doré, goes through five gates, and returns a ' +
      'figure tagged with the channel that produced it. Search proposes; ' +
      'deterministic code disposes. The trust signal is the chain, not the ' +
      'answer in isolation.'),
    el('div', { class: 'cp-doc-figure', html: svg }),
    el('p', { class: 'cp-doc-aside' },
      'Search results are re-ranked by authoritative-domain trust before ' +
      'they reach the validate gate — issuer transparency CDNs, regulators, ' +
      'major auditors, and on-chain explorers surface above generic ' +
      'content. A search-discovered figure stays at a lower trust tier ' +
      'until a second source confirms it.'));
  return wrap;
}

function renderCompendium(data, mount) {
  mount.innerHTML = '';
  const attCount = data.attestations.length;
  const withOverride = data.attestations.filter((a) => a.override_url).length;
  const withCache = data.attestations.filter((a) => a.cache_url).length;
  const liveSources = data.sources_health.filter((s) => s.status === 'live').length;
  const brokenSources = data.sources_health.filter((s) => s.status === 'broken').length;
  const unresolvedAttest = data.attestations.filter(
    (a) => !a.cache_url && !a.override_url).length;

  // Build a docs-style page: prose sections with live data embedded as
  // evidence. Goal: a human reads this top to bottom and understands how
  // Doré's data layer works AND what state it is in right now.
  const doc = el('article', { class: 'cp-doc fade-in' });
  mount.append(doc);

  // Header — written as a product page would be. Lead with what the
  // data layer DOES for the reader, not with engineering anatomy.
  doc.append(el('header', { class: 'cp-doc-head' },
    el('div', { class: 'cp-doc-kicker' }, 'Compendium'),
    el('h1', { class: 'cp-doc-title' },
      'The data behind every Doré answer.'),
    el('p', { class: 'cp-doc-lede' },
      'Doré reconciles what stablecoin issuers say against what the ' +
      'blockchains show. This page is the live ledger of that work: ' +
      'where each attestation URL comes from, which regulatory sources ' +
      'are reachable right now, what the background threads have ' +
      'discovered, and the immutable audit trail of every verified ' +
      'fact. Updated every thirty seconds.'),
    el('div', { class: 'cp-doc-meta' },
      el('span', {}, _fmtTs(new Date().toISOString())),
      el('span', { class: 'cp-doc-meta-sep' }, '·'),
      el('span', {}, attCount + ' stablecoins tracked'),
      el('span', { class: 'cp-doc-meta-sep' }, '·'),
      el('span', {}, data.sources_health.length + ' regulatory sources'),
    ),
  ));

  // Table-of-contents sidebar (sticky on wide viewports, inline on mobile).
  doc.append(el('nav', { class: 'cp-doc-toc' },
    el('div', { class: 'cp-toc-head' }, 'ON THIS PAGE'),
    el('ol', {},
      el('li', {}, el('a', { href: '#flow' },
        'How the data flows')),
      el('li', {}, el('a', { href: '#attestations' },
        'Attestation URLs')),
      el('li', {}, el('a', { href: '#canary' },
        'Corpus health (canary)')),
      el('li', {}, el('a', { href: '#discovery' },
        'Web discovery')),
      el('li', {}, el('a', { href: '#facts' },
        'Verified facts (audit trail)')),
      el('li', {}, el('a', { href: '#events' },
        'Background events')),
      el('li', {}, el('a', { href: '#refresh' },
        'How often things refresh')),
    ),
  ));

  // Short product-page-style paragraph with the figures inline. Scans
  // as a natural opening line rather than a six-cell dashboard.
  doc.append(el('section', { class: 'cp-doc-summary' },
    el('p', {},
      'Right now, ',
      el('b', {}, withCache + ' of ' + attCount),
      ' tracked stablecoins have a resolved attestation source. ',
      el('b', { class: 'cp-num-good' }, String(liveSources)),
      ' of ' + data.sources_health.length + ' regulatory sources are live. ',
      unresolvedAttest > 0
        ? el('span', {},
            el('b', { class: 'cp-num-neutral' }, String(unresolvedAttest)),
            ' tokens are still seeking a fresh attestation URL — those ' +
            'are listed in ',
            el('a', { href: '#attestations' }, 'Attestation URLs'),
            ' below, and the background thread tries to close each one ' +
            'every six hours.')
        : el('span', {},
            'Every tracked token has a resolved attestation source.'),
    ),
  ));

  // ── 0. How the data flows ──────────────────────────────────────────
  // Diagram + prose explainer that opens the page on capability,
  // before the dense tables. Inline SVG so it ships without external
  // image assets.
  doc.append(buildFlowSection());

  // ── 1. Attestation URLs ─────────────────────────────────────────────
  const attRows = data.attestations.slice().sort((a, b) => {
    const ar = (a.cache_url || a.override_url) ? 1 : 0;
    const br = (b.cache_url || b.override_url) ? 1 : 0;
    if (ar !== br) return ar - br;
    return a.symbol.localeCompare(b.symbol);
  });
  const attSection = el('section', { class: 'cp-doc-section', id: 'attestations' },
    el('h2', {}, 'Attestation URLs'),
    el('p', { class: 'cp-doc-para' },
      'Every fiat-backed stablecoin has a monthly attestation PDF published ' +
      'by an independent accountant. The URL of that PDF rotates — issuers ' +
      'change CDN paths, swap auditors, or switch transparency tools. Doré ' +
      'resolves a fresh URL through a six-step chain, then writes the ' +
      'winning URL through to the database so a curator-set value survives ' +
      'a redeploy. The YAML file is only a bootstrap seed.'),
    el('ol', { class: 'cp-doc-list' },
      el('li', {},
        el('b', {}, 'Store override.'),
        ' A curator (or the background discovery thread) sets a URL via ' +
        'POST /api/attestations/{symbol}/url. Stored in attestation_url_overrides.'),
      el('li', {},
        el('b', {}, 'In-process cache.'),
        ' attestation_cache.json, valid for 25 days.'),
      el('li', {},
        el('b', {}, 'YAML seed.'),
        ' config/stablecoins.yaml. Bootstrap-only.'),
      el('li', {},
        el('b', {}, 'Paxos resolver.'),
        ' Deterministic WordPress-CDN probes for PYUSD / USDP / USDG.'),
      el('li', {},
        el('b', {}, 'Web discovery.'),
        ' Typed search ("[symbol] reserves attestation [year] filetype:pdf"); ' +
        'HEAD-check before accepting.'),
      el('li', {},
        el('b', {}, 'Locator.'),
        ' Static / Gatsby / Next.js scrape of the issuer transparency page, ' +
        'LLM-ranked.')),
    el('p', { class: 'cp-doc-para' },
      'Below is every stablecoin Doré tracks. Tokens with no resolvable URL ' +
      'are listed first; tokens whose source is more than 35 days old are ' +
      'highlighted as stale (the staleness guardrail will flag those at the ' +
      'analysis surface too). The ' +
      el('span', { class: 'cp-via cp-via-store_override' }, 'override'),
      ' badge means the database is serving the URL; ',
      el('span', { class: 'cp-via cp-via-seed' }, 'seed'),
      ' means the YAML bootstrap; ',
      el('span', { class: 'cp-via cp-via-web_search' }, 'web_search'),
      ' means the background discovery thread found it.'),
    el('div', { class: 'cp-table' },
      el('div', { class: 'cp-row cp-head' },
        el('div', { class: 'cp-c1' }, 'TOKEN'),
        el('div', { class: 'cp-c2' }, 'ISSUER'),
        el('div', { class: 'cp-c3' }, 'CURRENT URL'),
        el('div', { class: 'cp-c4' }, 'VIA'),
        el('div', { class: 'cp-c5' }, 'AGE'),
      ),
      ...attRows.map((a) => {
        const url = a.override_url || a.cache_url || a.yaml_seed || '';
        const via = a.override_url ? (a.override_via || 'override')
          : (a.cache_via || (a.yaml_seed ? 'seed (unresolved)' : 'none'));
        const ts = a.override_set_at || a.cache_resolved_at;
        const days = _daysSince(ts);
        const cls = !url ? 'cp-warn'
          : (days !== null && days > 35) ? 'cp-stale' : '';
        return el('div', { class: 'cp-row ' + cls },
          el('div', { class: 'cp-c1' }, el('b', {}, a.symbol)),
          el('div', { class: 'cp-c2' }, a.issuer),
          el('div', { class: 'cp-c3' },
            url
              ? el('a', { href: url, target: '_blank', rel: 'noopener',
                  title: url }, url.length > 64 ? url.slice(0, 64) + '…' : url)
              : el('span', { class: 'cp-na' }, 'no URL resolved')),
          el('div', { class: 'cp-c4' },
            el('span', { class: 'cp-via cp-via-' + via.split(' ')[0] }, via)),
          el('div', { class: 'cp-c5' },
            days === null ? '—' : days + 'd'),
        );
      }),
    ),
  );
  doc.append(attSection);

  // ── 2. Corpus health (canary) ──────────────────────────────────────
  const broken = data.sources_health.filter((s) => s.status === 'broken');
  const unknown = data.sources_health.filter((s) => s.status === 'unknown');
  const live = data.sources_health.filter((s) => s.status === 'live')
    .sort((a, b) => (a.fetched_at < b.fetched_at ? 1 : -1));
  const srcSection = el('section', { class: 'cp-doc-section', id: 'canary' },
    el('h2', {}, 'Corpus health'),
    el('p', { class: 'cp-doc-para' },
      'The reasoning corpus is a curated set of regulatory and policy sources ' +
      'every analysis cites against (GENIUS Act, MiCA Title III, FSB ' +
      'recommendations, OFAC SDN actions, BIS speeches, Chainalysis posts). ' +
      'Every source is included by default — opt-out, not opt-in — so a ' +
      'human only intervenes to exclude a source or mark one explicitly ' +
      'verified. A background canary thread re-fetches each source every ' +
      'six hours, archives a snapshot, and emits ' +
      el('code', {}, 'health.source.flipped'),
      ' the moment a source transitions live ↔ broken. When a source goes ' +
      'broken, the UI silently serves the archived copy and flags the gap.'),
    broken.length > 0
      ? el('p', { class: 'cp-doc-para cp-doc-alert' },
          el('b', {}, broken.length + ' source(s) are currently broken.'),
          ' Listed first below. Doré is serving the archived snapshots ' +
          'transparently; a maintainer should re-anchor them.')
      : el('p', { class: 'cp-doc-para' },
          'All sources are currently live (' + live.length + ' tracked).'),
    el('div', { class: 'cp-table' },
      el('div', { class: 'cp-row cp-head' },
        el('div', { class: 'cp-c1' }, 'STATUS'),
        el('div', { class: 'cp-c2' }, 'SOURCE'),
        el('div', { class: 'cp-c3' }, 'URL'),
        el('div', { class: 'cp-c5' }, 'AGE'),
      ),
      ...[...broken, ...unknown, ...live].slice(0, 60).map((s) =>
        el('div', { class: 'cp-row ' +
          (s.status === 'broken' ? 'cp-warn'
            : s.status === 'unknown' ? 'cp-stale' : '') },
          el('div', { class: 'cp-c1' },
            el('span', { class: 'cp-via cp-via-' + s.status }, s.status)),
          el('div', { class: 'cp-c2' }, s.title || s.id),
          el('div', { class: 'cp-c3' },
            s.url
              ? el('a', { href: s.url, target: '_blank', rel: 'noopener',
                  title: s.url }, s.url.length > 56 ? s.url.slice(0, 56) + '…' : s.url)
              : el('span', { class: 'cp-na' }, '—')),
          el('div', { class: 'cp-c5' },
            s.age_days === null || s.age_days === undefined
              ? '—' : s.age_days + 'd'),
        )),
    ),
  );
  doc.append(srcSection);

  // ── 3. Web discovery ───────────────────────────────────────────────
  const discSection = el('section', { class: 'cp-doc-section', id: 'discovery' },
    el('h2', {}, 'Web discovery'),
    el('p', { class: 'cp-doc-para' },
      'When the locator cannot reach a JS-rendered transparency page, Doré ' +
      'runs a typed web search for a fresh attestation PDF, HEAD-checks ' +
      'every candidate, and writes the winner through to the store. The ' +
      'same rule a researcher uses by hand — "search the issuer name plus ' +
      'attestation plus the year, look for a /wp-content/ PDF" — automated ' +
      'and logged. Every discovery is visible below with the query and the ' +
      'provider that returned it.'),
    el('p', { class: 'cp-doc-para' },
      'Backend currently: ',
      el('b', { class: data.discovery_provider ? 'cp-num-good'
        : 'cp-num-neutral' },
        data.discovery_provider
          ? data.discovery_provider
          : 'disabled (set SCA_WEB_SEARCH_PROVIDER and SCA_WEB_SEARCH_KEY)'),
      '. Recent hits: ',
      el('b', {}, String(data.discoveries.length)), '.'),
    data.discoveries.length === 0
      ? el('p', { class: 'cp-doc-para cp-doc-aside' },
          'No discoveries recorded yet. When the resolver hits a gap and a ' +
          'search backend is configured, hits appear here with the URL and ' +
          'the query that found them.')
      : el('div', { class: 'cp-table' },
          el('div', { class: 'cp-row cp-head' },
            el('div', { class: 'cp-c1' }, 'TOKEN'),
            el('div', { class: 'cp-c3' }, 'URL'),
            el('div', { class: 'cp-c4' }, 'PROVIDER'),
            el('div', { class: 'cp-c5' }, 'WHEN'),
          ),
          ...data.discoveries.map((d) => el('div', { class: 'cp-row' },
            el('div', { class: 'cp-c1' }, el('b', {}, d.symbol)),
            el('div', { class: 'cp-c3' },
              el('a', { href: d.url, target: '_blank', rel: 'noopener',
                title: d.url },
                d.url.length > 64 ? d.url.slice(0, 64) + '…' : d.url)),
            el('div', { class: 'cp-c4' }, d.provider || d.via),
            el('div', { class: 'cp-c5' }, _fmtTs(d.resolved_at)),
          )),
      ),
  );
  doc.append(discSection);

  // ── 4. Verified facts (audit trail) ────────────────────────────────
  const facts = data.verified_facts || [];
  const factsSection = el('section', { class: 'cp-doc-section', id: 'facts' },
    el('h2', {}, 'Verified facts (audit trail)'),
    el('p', { class: 'cp-doc-para' },
      'Every successful analysis writes one or more rows into a separate, ' +
      'append-only table: supply per chain, reserves per token. Each row ' +
      'carries the claim type, the subject, the observed value, the sources ' +
      'reconciled against, the block number, the timestamp, and a ' +
      'sha256 content hash. Corrections insert a new row and link the prior ' +
      'one as superseded; nothing is ever updated in place. This is the ' +
      'audit trail that makes point-in-time replay possible: ask "what did ' +
      'Doré report for USDC on 2026-04-30?" and the answer is a SQL window ' +
      'query, not a guess.'),
    el('p', { class: 'cp-doc-para cp-doc-aside' },
      'The schema is intentionally generic. The same table will hold the ' +
      'next claim type (agent payments) without a schema change — only ' +
      'a new value of ',
      el('code', {}, 'claim_type'), '.'),
    facts.length === 0
      ? el('p', { class: 'cp-doc-para cp-doc-aside' },
          'No verified facts recorded yet in this process. The audit trail ' +
          'populates on every successful analysis.')
      : el('div', { class: 'cp-table' },
          el('div', { class: 'cp-row cp-head' },
            el('div', { class: 'cp-c1' }, 'CLAIM'),
            el('div', { class: 'cp-c2' }, 'SUBJECT'),
            el('div', { class: 'cp-c3' }, 'VALUE'),
            el('div', { class: 'cp-c4' }, 'STATUS'),
            el('div', { class: 'cp-c5' }, 'OBSERVED'),
          ),
          ...facts.map((f) => {
            const v = f.value || {};
            let summary = '';
            if (f.claim_type === 'supply') {
              summary = (v.supply !== undefined
                ? Number(v.supply).toLocaleString(undefined,
                    { maximumFractionDigits: 0 })
                : '—') + ' on ' + (f.chain || '?');
            } else if (f.claim_type === 'reserves') {
              summary = '$' + (v.total_reserves_usd !== undefined
                ? Number(v.total_reserves_usd).toLocaleString(undefined,
                    { maximumFractionDigits: 0 })
                : '—') + ' as of ' + (v.as_of_date || '?');
            } else {
              summary = JSON.stringify(v).slice(0, 60);
            }
            return el('div', { class: 'cp-row' },
              el('div', { class: 'cp-c1' },
                el('span', { class: 'cp-via cp-via-' + f.claim_type },
                  f.claim_type)),
              el('div', { class: 'cp-c2' }, f.subject),
              el('div', { class: 'cp-c3' }, summary),
              el('div', { class: 'cp-c4' },
                el('span', { class: 'cp-via cp-via-' + f.status }, f.status)),
              el('div', { class: 'cp-c5' }, _fmtTs(f.observed_at)),
            );
          }),
      ),
  );
  doc.append(factsSection);

  // ── 5. Background events ───────────────────────────────────────────
  const evSection = el('section', { class: 'cp-doc-section', id: 'events' },
    el('h2', {}, 'Background events'),
    el('p', { class: 'cp-doc-para' },
      'Every external call Doré makes — RPC reads, attestation fetches, ' +
      'OFAC SDN downloads, web-discovery queries, canary sweeps — emits a ' +
      'structured event through ',
      el('code', {}, 'sca.observability.log_event'),
      '. The most recent 500 events live in a ring buffer that this page ' +
      'reads from. Below is the tail filtered to the kinds that warrant ' +
      'attention — discovery hits, source flips, RPC consensus degradation, ' +
      'OFAC fetch failures.'),
    data.events.length === 0
      ? el('p', { class: 'cp-doc-para cp-doc-aside' },
          'No background events recorded yet. The canary and discovery ' +
          'thread will surface theirs here within the next sweep cycle.')
      : el('div', { class: 'cp-events' },
          ...data.events.map((e) => el('div', {
            class: 'cp-event cp-lvl-' + (e.level || 'info'),
          },
            el('span', { class: 'cp-ev-ts' }, _fmtTs(
              new Date((e.ts || 0) * 1000).toISOString())),
            el('span', { class: 'cp-ev-kind' }, e.kind || ''),
            el('span', { class: 'cp-ev-lvl' }, (e.level || 'info').toUpperCase()),
            el('span', { class: 'cp-ev-detail' },
              (e.symbol ? '[' + e.symbol + '] ' : '') +
              (e.detail || e.error_message || e.url || '')),
          ))),
  );
  doc.append(evSection);

  // ── 6. How often things refresh ────────────────────────────────────
  doc.append(el('section', { class: 'cp-doc-section', id: 'refresh' },
    el('h2', {}, 'How often things refresh'),
    el('p', { class: 'cp-doc-para' },
      'Doré is designed for sustained operation — the background thread ' +
      'keeps the data layer current so user-facing reads can be served from ' +
      'cache without lying. Each artefact has its own refresh cadence:'),
    el('table', { class: 'cp-refresh' },
      el('thead', {}, el('tr', {},
        el('th', {}, 'Artefact'),
        el('th', {}, 'How often it refreshes'),
        el('th', {}, 'Force-refresh path'),
      )),
      el('tbody', {},
        el('tr', {},
          el('td', {}, 'Live on-chain supply'),
          el('td', {}, '60-second in-process cache; re-read on demand'),
          el('td', {}, 'Reload the page'),
        ),
        el('tr', {},
          el('td', {}, 'Attestation URL resolution'),
          el('td', {}, '25-day store; background gap-sweep every 6 hours'),
          el('td', {}, '"Refresh analysis" button (analyze surface)'),
        ),
        el('tr', {},
          el('td', {}, 'Attestation PDF + extraction'),
          el('td', {}, 'Per analysis run; cached 6h server-side'),
          el('td', {}, '"Refresh analysis" button'),
        ),
        el('tr', {},
          el('td', {}, 'OFAC SDN list'),
          el('td', {}, 'Daily fetch; >7 days raises critical guardrail'),
          el('td', {}, el('code', {}, 'sca refresh')),
        ),
        el('tr', {},
          el('td', {}, 'Corpus source health (canary)'),
          el('td', {}, 'Every 6 hours (SCA_HEALTH_INTERVAL_HOURS)'),
          el('td', {}, el('code', {}, 'sca canary')),
        ),
        el('tr', {},
          el('td', {}, 'Corpus discovery (new sources)'),
          el('td', {}, 'Daily cron'),
          el('td', {}, el('code', {}, 'sca discover')),
        ),
        el('tr', {},
          el('td', {}, 'Verified facts (audit trail)'),
          el('td', {}, 'Written on every analysis; never overwritten'),
          el('td', {}, 'n/a — append-only'),
        ),
      ),
    ),
    el('p', { class: 'cp-doc-aside' },
      'Anyone wanting an immediate re-read can hit the "refresh" action on ' +
      'any surface — that bypasses every cache. The longer in-app caches ' +
      'exist because the background thread keeps the underlying data fresh, ' +
      'not because we are trying to hide staleness.'),
  ));
}

// ════════════════════════════════════════════════════════════════════
//  ROUTER
// ════════════════════════════════════════════════════════════════════
function route() {
  const hash = location.hash.replace(/^#/, '') || 'monitor';
  const [view, arg] = hash.split('/');
  document.querySelectorAll('.side-row').forEach((l) =>
    l.classList.toggle('active', l.dataset.route === view));
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (surfacePollTimer) { clearInterval(surfacePollTimer); surfacePollTimer = null; }
  if (compendiumTimer) { clearInterval(compendiumTimer); compendiumTimer = null; }
  if (view === 'analyze') {
    viewAnalyze(arg ? arg.toUpperCase() : '');
  } else if (view === 'sanctions') {
    viewSurface('sanctions', arg ? arg.toUpperCase() : '');
  } else if (view === 'redemptions') {
    viewSurface('redemptions', arg ? arg.toUpperCase() : '');
  } else if (view === 'analyst') {
    STATE.activeSymbol = '';
    refreshInstruments();
    viewAnalyst();
  } else if (view === 'compendium') {
    // Compendium now lives as a standalone docs page at /compendium —
    // opens in a new tab via the sidebar link. If a user lands here
    // via the in-app hash anyway, redirect them to the standalone page.
    window.open('/compendium', '_blank', 'noopener');
    location.hash = '#monitor';
    viewMonitor();
  } else {
    STATE.activeSymbol = '';
    refreshInstruments();
    if (view === 'corpus') viewCorpus();
    else if (view === 'evals') viewEvals();
    else viewMonitor();
  }
}

// ════════════════════════════════════════════════════════════════════
//  BOOT
// ════════════════════════════════════════════════════════════════════
async function boot() {
  // status bar
  tickClock();
  setInterval(tickClock, 1000);
  pollHealth();
  setInterval(pollHealth, 20000);

  // optional auth — restore any existing session, init the sign-in control.
  // Never blocks: the terminal is fully usable anonymously.
  try { await AUTH.init(); } catch (e) { /* anonymous mode is the fallback */ }

  // command line
  const ci = $('#cmd-input');
  ci.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { runCommand(ci.value); ci.value = ''; }
  });
  // F-key navigation
  window.addEventListener('keydown', (e) => {
    const map = { F1: '#monitor', F2: '#analyze', F3: '#corpus', F4: '#evals',
      F5: '#sanctions', F6: '#redemptions', F7: '#analyst', F8: '#compendium' };
    if (map[e.key]) { e.preventDefault(); location.hash = map[e.key]; }
    if (e.key === '/' && document.activeElement !== ci) { e.preventDefault(); ci.focus(); }
  });

  // sidebar nav links — let target="_blank" or modifier-clicked links
  // through to the browser; only intercept same-tab hash routes.
  document.querySelectorAll('.side-row').forEach((l) => {
    l.addEventListener('click', (e) => {
      if (l.target === '_blank' || e.metaKey || e.ctrlKey ||
          e.shiftKey || e.button !== 0) {
        return;
      }
      e.preventDefault();
      location.hash = '#' + l.dataset.route;
    });
  });

  window.addEventListener('hashchange', route);

  // first render so something is on screen immediately
  route();

  // load the registry, then arm the live monitor
  logLine('WORK', 'BOOT', [seg('session up', 'lg-val'), seg('loading registry')]);
  // warm the corpus source registry in the background — the REASONING FRAME
  // passage cards look up source titles + URLs from it. Non-blocking; the
  // renderer has a humanised-id fallback for the (rare) pre-load case.
  loadSources();
  try {
    const data = await api('/tokens');
    STATE.tokens = data.tokens;
    refreshInstruments();
    refreshGrid();
    refreshTicker();
    // if the user landed on the F5/F6 no-token picker before the registry
    // resolved, re-render the view so the picker fills with instruments.
    const [v, a] = location.hash.replace(/^#/, '').split('/');
    if (!a && !STATE.activeSymbol &&
        (v === 'sanctions' || v === 'redemptions')) {
      route();
    }
    logLine('OK', 'REGISTRY', [
      seg('loaded', 'lg-val'),
      seg('tok' + data.count, 'd-up'),
      seg('surveillance armed'),
    ]);
    startMonitor();
  } catch (e) {
    logLine('ERR', 'REGISTRY', [seg('LOAD-FAIL', 'd-warn'),
      seg(String(e.message).slice(0, 48))]);
    const box = $('#side-instr');
    if (box) box.innerHTML = '<div class="side-empty">registry unavailable</div>';
  }
}

window.addEventListener('DOMContentLoaded', boot);

// ── Model-tier toggle ─────────────────────────────────────────────────
// FAST default (~1-2s LLM hops via deepseek-v4-flash). DEEP opt-in
// (~5-30s via the reasoning model) — UI shows a 120s countdown badge
// while a deep run is in flight so the user knows the budget.
// Persists across reloads in localStorage; surfaced on every request.
const _TIER_KEY = 'dore.modelTier';
function getTier() {
  try { return localStorage.getItem(_TIER_KEY) === 'deep' ? 'deep' : 'fast'; }
  catch (_e) { return 'fast'; }
}
function setTier(tier) {
  try { localStorage.setItem(_TIER_KEY, tier); } catch (_e) { /* ignore */ }
  renderTierToggle();
}
function renderTierToggle() {
  const tier = getTier();
  document.querySelectorAll('.tier-btn').forEach((btn) => {
    if (btn.dataset.tier === tier) btn.classList.add('tier-active');
    else btn.classList.remove('tier-active');
  });
  const wrap = document.getElementById('topbar-tier');
  if (wrap) {
    wrap.classList.toggle('tier-deep-active', tier === 'deep');
    wrap.title = tier === 'deep'
      ? 'PRO mode active — uses the deeper-reasoning model (slower; up to '
        + '120s per LLM hop). Better at multi-step reasoning over the corpus. '
        + 'Click FLASH to switch back to the default fast model.'
      : 'FLASH mode (default) — fast model (~1-2s per LLM hop). Click PRO '
        + 'to opt into the deeper-reasoning model for harder questions.';
  }
}
// Hook up the buttons + initial render.
function wireTierToggle() {
  document.querySelectorAll('.tier-btn').forEach((btn) => {
    btn.addEventListener('click', () => setTier(btn.dataset.tier));
  });
  renderTierToggle();
}
window.addEventListener('DOMContentLoaded', wireTierToggle);

// ── Theme toggle ──────────────────────────────────────────────────────
// Sun ↔ moon icon in the topbar; persists to localStorage. Default is
// dark (Bloomberg-terminal aesthetic). Light mode re-tunes the palette
// while preserving brand identity (gold accent stays gold, just darker
// brass for AA contrast on cream).
const _THEME_KEY = 'dore.theme';
function getTheme() {
  try { return localStorage.getItem(_THEME_KEY) === 'light' ? 'light' : 'dark'; }
  catch (_e) { return 'dark'; }
}
function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
}
function setTheme(theme) {
  try { localStorage.setItem(_THEME_KEY, theme); } catch (_e) { /* ignore */ }
  applyTheme(theme);
}
// Apply theme BEFORE DOMContentLoaded so there's no white-flash on a
// dark-mode user's reload.
(function initTheme() { applyTheme(getTheme()); })();
function wireThemeToggle() {
  const btn = document.getElementById('topbar-theme');
  if (!btn) return;
  btn.addEventListener('click', () => {
    setTheme(getTheme() === 'light' ? 'dark' : 'light');
  });
}
window.addEventListener('DOMContentLoaded', wireThemeToggle);

// Pro-mode countdown ring — when an analyze / sanctions / redemption job is
// running on DEEP tier, show a 120s countdown badge near the toggle so the
// user knows the budget. Cleared when the job finishes.
let _deepCountdownTimer = null;
function startDeepCountdown(deadlineMs) {
  const wrap = document.getElementById('topbar-tier');
  if (!wrap) return;
  stopDeepCountdown();
  let badge = document.getElementById('tier-countdown');
  if (!badge) {
    badge = document.createElement('span');
    badge.id = 'tier-countdown';
    badge.className = 'tier-countdown';
    wrap.appendChild(badge);
  }
  const tick = () => {
    const remaining = Math.max(0, Math.ceil((deadlineMs - Date.now()) / 1000));
    badge.textContent = remaining + 's';
    if (remaining <= 0) stopDeepCountdown();
  };
  tick();
  _deepCountdownTimer = setInterval(tick, 1000);
}
function stopDeepCountdown() {
  if (_deepCountdownTimer) {
    clearInterval(_deepCountdownTimer);
    _deepCountdownTimer = null;
  }
  const badge = document.getElementById('tier-countdown');
  if (badge) badge.remove();
}
