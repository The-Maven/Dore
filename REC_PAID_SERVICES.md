# Paid services — investment recommendation for next quarter

Honest assessment from inside the codebase. Each section names the
concrete capability we *cannot* deliver today on the free tier, the
service that closes the gap, and an order-of-magnitude monthly cost.
Decisions are yours.

What we have today, free, that works:
- Coinbase v2 + Kraken + CoinGecko free tier — 3 peg sources, parallel polling, 5bp agreement gate.
- Brave Search free tier — 200 calls/day, gated by interest, cached 12h with stale-on-error.
- Self-hosted Supabase Postgres on the developer's machine.
- Local-disk snapshot store.
- Anthropic API on a small monthly cap for judge + commentary.
- Free public RPCs (Cloudflare-eth, llamarpc).

This stack got us to a working F1–F9 product. The ceilings that
matter for going to a YC pitch or a paid pilot are below, sorted by
impact-per-dollar.

---

## Tier 1 — capabilities that are gates, not nice-to-haves

### 1. Production data feeds: **Pyth Network Hermes** (free → paid) + **Chronicle Labs / Chainlink Data Streams** (~$0–$5k/mo at our cadence)

What we have: spot prices from CB / Kraken / CoinGecko polled every 1m.
What we cannot deliver: sub-second peg deviation, on-chain attestation of the price reading itself, oracle-grade lineage.

Pyth Hermes is currently free for the Pyth-network feed (the same one used by Aave V3, Solend, etc). The paid tier adds higher request budgets + SLA. Pulling it lets us tag every peg tick with "Pyth confirmed at slot X" — which is the language institutional readers expect.

Chainlink Data Streams is the institutional alternative. ~$3–5k/mo at our usage; the win is regulatory legibility — when a stablecoin issuer wants to license our intelligence, "Chainlink-sourced" is a stamp procurement teams already accept.

**Concrete elevation:** the calibration archive earns the credibility line we currently can't ship — "every prediction scored against an oracle-grade price feed, not a polled exchange rate."

**Cost:** $0–$5k/mo. Recommend starting with Pyth (free, immediate) and add Chainlink only when we have a customer asking for it.

---

### 2. Premium LLM tier: **Anthropic Claude Opus** for the judge, **Sonnet** for commentary (~$200–800/mo)

What we have: a small budget on Claude that we throttle hard — judge runs on the largest mover only, commentary cached 1h per regime.

What we cannot deliver: judge prose on every active token every cycle, richer reasoning chains, longer cited grounding context.

Sonnet at full budget runs the judge on every active token (×18 tokens × 6 cycles/hour × 24 hours = 2.6k calls/day). At ~$3/Mtok and ~1.5k tokens per call: ~$10/day = $300/mo.

Opus for the judge ($15/Mtok input, ~7×Sonnet cost): $200/mo if scoped to flagged tokens (cone exceeding alert threshold), ~$2k/mo if every token.

**Concrete elevation:** the AI Judge stops being "reserved for the biggest mover" and becomes a per-token current read. The "Awaiting LLM synthesis on the next tick" empty state goes away entirely.

**Cost:** $200–800/mo depending on aggressiveness. Recommend $300/mo Sonnet across the board + $200/mo Opus reserved for alert-state tokens.

---

### 3. Managed Supabase Pro: **$25/mo per project**

What we have: developer-local Postgres with migrations 0001–0008 applied. No backups, no point-in-time recovery, no read replicas. Works for one developer; will not survive an actual user load.

What we cannot deliver: durable calibration archive that survives a workstation crash, multi-region read replicas, daily backups, RLS-aware monitoring.

**Concrete elevation:** the "calibration archive" stops being a marketing claim and becomes an auditable artifact a regulator or auditor could subpoena. Snapshot store moves off local disk to Supabase Storage — same provider, durable, no infra work.

**Cost:** $25/mo today; $599/mo at the Team tier when we need multi-user RBAC. Easy yes.

---

## Tier 2 — strong wins, but not gates

### 4. Brave Search Pro: **$3–9/mo for ~2k–10k queries/mo**

What we have: 200 free queries/day, gated by interest, 12h cached. In practice we burn ~10/day because the gate is tight.

What we cannot deliver: aggressive expansion of the corpus discovery sweep, per-token issuer-news searches on every cycle.

**Concrete elevation:** the news enrichment becomes proactive. Today an issuer's PR can sit unaddressed for 12 hours; with paid Brave we can run an issuer-status search every 30 minutes per token.

**Cost:** $9/mo for 10k queries. Buy it.

---

### 5. **Tavily** or **Exa** for structured web research: **~$50/mo**

What we have: Brave returns URLs + snippets. The judge has to read snippets and gamble.

What we cannot deliver: deep-fetched, denoised, citation-ranked web context for the agent at F7.

Tavily Pro ($30/mo for 4k queries) does the URL-fetch-and-clean step server-side; Exa ($50/mo) does semantic search across structured corpuses.

**Concrete elevation:** F7 ANALYST goes from "asks Brave then guesses" to "asks Tavily, gets clean inputs, the agent's answer cites three primary sources by default."

**Cost:** $50/mo for either. Recommend Tavily for the clean-fetch behavior we need first.

---

### 6. **Nansen Pro** for on-chain attribution: **~$2k/mo**

What we have: nothing. We do not consume on-chain analytics today.

What we cannot deliver: "USDe just lost $50M of supply to address 0x... — labelled Wintermute hot wallet."

**Concrete elevation:** the AI Judge can name attribution. "USDe minting concentrated in three Wintermute hot wallets" is the kind of read paid customers (funds, market makers) actively pay for.

**Cost:** $2k/mo for the API tier. Justified only when we have at least one paying customer. Defer to Q4 if the user-base accelerates.

---

### 7. **Dune Analytics API** for cohort intelligence: **$400–1k/mo**

What we have: nothing systematic. Spot-checked by hand.

What we cannot deliver: "USDC vs USDT 30-day flow asymmetry," "PSM utilisation curve for DAI," "Curve 3pool basis time series."

**Concrete elevation:** every F2 ANALYZE result page can carry a small cohort chart sourced from a curated Dune query. Visually richer, far more credible.

**Cost:** $400/mo at Plus tier, $1k/mo at Premium. Recommend Plus.

---

### 8. **Chainalysis Crypto Investigations** for sanctions intelligence: **$3–10k/mo**

What we have: free OFAC SDN list, manual canary, free Chainalysis sanctions oracle endpoint.

What we cannot deliver: real-time sanctions screening at scale, KYT (know-your-transaction) for transfer flows, jurisdiction-specific risk scoring.

**Concrete elevation:** F5 SANCTIONS goes from "looked up against the OFAC list" to "Chainalysis-verified, with a jurisdiction-specific risk score and a citation we can defend in court."

**Cost:** $3k/mo at the smallest paid tier. Only worth it when a paying customer asks (banks, payment processors).

---

## Tier 3 — production infra, when we have users

### 9. **Fly.io** / **Render** for hosting: **$25–100/mo**

What we have: local uvicorn. No HTTPS, no public URL, no horizontal scale.

**Concrete elevation:** the product becomes shareable. A YC partner can hit `dore.ai` instead of "let me share my screen."

**Cost:** $25/mo at minimum. Buy it the day we have a customer demo.

---

### 10. **Sentry** for error monitoring: **$26/mo team tier**

What we have: structured `log_event` events written to local logs. No alerting, no aggregation, no dashboards.

**Concrete elevation:** the moment something breaks in production, we get a Slack DM before the user notices.

**Cost:** $26/mo. Buy day one of production.

---

### 11. **Resend** for transactional email: **$20/mo**

What we have: no email anywhere.

**Concrete elevation:** when a stablecoin de-pegs past a customer's alert threshold, we send them an email. The hook that makes the product sticky beyond curiosity.

**Cost:** $20/mo at the 50k-emails tier. Buy when we have a paid pilot.

---

## DATA SOURCES — the never-sketchy upgrade path

Production-grade trading is downstream of production-grade data. The
current free tier is OK for a demo; for paid customers (or any
real-money simulation) the source mix needs to be hardened. Ranked
by impact-per-dollar:

### Free / immediately-deployable (no $ required)

1. **Pyth Network Hermes — ALREADY ADDED.** Real-time, oracle-grade,
   first-party aggregated feeds (90+ publishers including Jane Street,
   Two Sigma, DRW, GTS). The strongest single addition: independent
   of every CEX we already poll, sub-second freshness, no rate limit
   we've ever observed at our cadence. Free pull endpoint at
   `hermes.pyth.network`. Done; integrated.

2. **Binance Spot API** (free, no auth required for tickers): adds
   another major CEX outside the Coinbase + Kraken duopoly we already
   poll. Useful for FDUSD (Binance-dominant) and any depeg event
   that originates on Asia-session venues. Rate limit ~1,200 req/min
   on the public tier. Symbol convention: `USDCUSDT`, `USDTBUSD` etc.
   Recommend adding next.

3. **OKX Spot API** (free, no auth): same shape as Binance,
   covers Asia + EU spot. Adds a third independent CEX. Quick win.

### Paid sources (worth the spend when revenue justifies)

4. **Chainlink Data Feeds** (~$3-5k/mo): the institutional reference
   that procurement teams already accept. On-chain price feeds with
   defined update heartbeats. The "regulator-legible" stamp.

5. **Curve Finance LLAMMA / pool reads** (free via public RPC, but
   compute-intensive): direct AMM prices from on-chain Curve 3pool,
   crvUSD LLAMMA, FRAX bp, etc. Truly direct, no intermediary. For
   DeFi-native tokens (FRAX, GHO, crvUSD, LUSD, USDD), this is the
   highest-quality source.

6. **DefiLlama Pro** ($300/mo): aggregates 200+ DEX pools, useful for
   tokens with major Uniswap V3 / Balancer / Curve liquidity.

7. **CoinGecko Pro** ($129/mo): higher rate limit (500 req/min vs
   30) eliminates the rate-limit issue we currently see. **Note:**
   not as important as adding Pyth + Binance because we'd still be
   single-aggregator on edge cases. The Pro tier is a bandaid, not
   a fix.

### Hard rule we just enforced

The "never-sketchy" guarantee: the trader REFUSES to enter when the
freshest available source for a symbol is > 90 seconds old. The
default visible-data marker is STALE (no number) when the latest
tick is > 3 minutes old. Money at stake = no off-market entries on
unrefreshed data.

The Pyth integration is the structural fix for this. Where CoinGecko
was the only source and rate-limited to 60s cache, Pyth is sub-second
and parallel. With Pyth now in the mix, the STALE outcomes should
disappear for the universe Pyth covers (USDC, USDT, DAI, PYUSD, USDP,
TUSD, FDUSD, FRAX, GUSD — the main fiat-backed + algorithmic majors).

## Recommended Q3 spend, ranked by ROI

| Service | Monthly | Lock-in | Buy when |
| --- | --- | --- | --- |
| Pyth Hermes | **Free** | None | **Immediately** — gates "oracle-grade" framing |
| Supabase Pro | $25 | Low | **Immediately** — durability gate |
| Anthropic Sonnet (commentary) | $300 | None | **Immediately** — kills "loading…" UX |
| Brave Pro | $9 | None | **Immediately** — under $10 |
| Tavily Pro | $30 | Low | **Q3** — when F7 ANALYST gets demo'd |
| Anthropic Opus (alert judge) | $200 | None | **Q3** — when we onboard a pilot |
| Dune Plus | $400 | Low | **Q4** — when we have one paying customer |
| Sentry | $26 | None | **Q4** — when we go public |
| Fly.io | $25 | None | **Q4** — when we go public |
| Resend | $20 | Low | **Q4** — when we ship alerts |
| Nansen Pro | $2,000 | High | **Q1 2027** — when revenue justifies |
| Chainlink Data Streams | $5,000 | High | **Q2 2027** — when a regulated buyer requires it |
| Chainalysis | $3,000–10,000 | High | **Q2 2027** — when a bank or PSP buyer requires it |

**Q3 baseline spend: ~$334/mo** for the four "immediately" items.

**Q3 + early-customer spend: ~$564/mo** adding Opus + Tavily.

**Q4 production spend: ~$1,035/mo** adding Sentry + Fly + Resend + Dune.

**Q1 2027 institutional spend: ~$3,000/mo** adding Nansen when we have at least one paying customer.

---

## What I would NOT recommend buying yet

- **Bloomberg Terminal data**: $24k/year, locked behind enterprise contracts, useless when our UI is the differentiator.
- **CoinGecko Pro**: free tier is more than enough at our cadence. Don't pay.
- **Anthropic Enterprise**: rate limits on the standard tier are already loose; the Enterprise tier is for compliance, not capability.
- **Datadog**: Sentry is enough. Datadog at $15+/host/mo is for infra ops we won't have for two quarters.

---

## The honest gap

The biggest constraint today is NOT money. It is the **calibration archive thinness** — until we have 100+ resolved predictions per token, no scoring chart we render carries weight. The fix for that is time (the ticker has to run for ~2 weeks at 10-minute cadence), not spend. Buying Pyth + Supabase Pro lets us start that clock with confidence; everything else amplifies what's already trustworthy.

If you spend $100/mo on Q3 (Supabase + Brave + maybe a small Anthropic top-up), the platform is meaningfully better. If you spend $500/mo, the AI loading UX is gone and the calibration archive is durable. Anything beyond that is a customer-driven decision, not a product-driven one.
