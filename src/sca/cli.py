"""sca — terminal interface, consultancy gold/dark theme.

  sca analyze SYMBOL    full attestation analysis
  sca supply SYMBOL     on-chain supply only
  sca tokens            list configured stablecoins
  sca sources           list corpus sources + curation status
  sca curate            vote: approve sources, verify addresses
  sca refresh           re-resolve attestation URLs from transparency pages
  sca evals             run the eval harness
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from sca import config
from sca import theme as t
from sca import votes
from sca.agent import analyze as run_analyze
from sca.agent import assess_redemption, screen_token
from sca.corpus.ingest import ingest_source
from sca.corpus.sources import all_sources
from sca.evals import run_evals
from sca.models import Analysis, SupplyResult
from sca.tools import AttestationUnavailable, get_onchain_supply, resolve_url


def _header(subtitle: str) -> None:
    print()
    print(f"  {t.brandmark()}  {t.paint('RAYLEIGH STARK', t.PAPER, bold=True)}"
          f"  {t.paint('stablecoin compliance agent', t.MUTED)}")
    print(f"  {t.eyebrow(subtitle)}")
    print(f"  {t.rule()}")


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _emit_json(obj) -> None:
    """Machine-readable structured output — dataclasses serialised verbatim."""
    print(json.dumps(dataclasses.asdict(obj), indent=2, default=str))


# ── renderers ─────────────────────────────────────────────────────────
def render_supply(supply: SupplyResult) -> None:
    print(f"  {t.icon('supply')} {t.paint('ON-CHAIN SUPPLY', t.PAPER, bold=True)}"
          f"  {t.paint(supply.symbol, t.GOLD)}")
    print()
    print(t.kv("native supply", t.paint(_money(supply.native_supply), t.PAPER)))
    if supply.bridged_supply > 0:
        share = supply.bridged_share
        suffix = f"  ({share * 100:.1f}% of gross)" if share else ""
        print(t.kv("bridged supply",
                   t.paint(_money(supply.bridged_supply), t.AMBER) + suffix))
    print(t.kv("headline (native)",
               t.paint(_money(supply.total_supply), t.PAPER)))
    print()
    print(f"  {t.paint('PER CHAIN · PROVENANCE', t.GOLD)}")
    for c in supply.per_chain:
        vmark = t.icon("ok") if c.verified else t.icon("warn")
        kind = t.paint(c.kind, t.GOLD if c.kind == "native" else t.AMBER)
        print(f"  {vmark} {t.paint(c.chain.ljust(10), t.PAPER)} "
              f"{t.paint(_money(c.supply).rjust(20), t.MUTED)}  {kind}")
    for warning in supply.warnings:
        print(f"  {t.icon('warn')} {t.paint(warning, t.AMBER)}")
    if supply.read_at:
        print(f"  {t.paint('read at ' + supply.read_at, t.MUTED, dim=True)}")
    print()


def render_analysis(analysis: Analysis) -> None:
    a = analysis
    print(f"  {t.heading('01', 'attestation analysis')}"
          f"   {t.paint(a.symbol, t.GOLD, bold=True)}")
    print()

    # Snapshot
    print(f"  {t.icon('metric')} {t.paint('SNAPSHOT', t.PAPER, bold=True)}")
    print(t.kv("on-chain supply", t.paint(_money(a.supply.total_supply), t.PAPER)))
    if a.supply.bridged_supply > 0:
        print(t.kv("  of which bridged",
                   t.paint(_money(a.supply.bridged_supply) + " (excluded)",
                           t.AMBER)))
    if a.attestation is not None:
        att = a.attestation
        print(t.kv("attested reserves", t.paint(_money(att.total_reserves), t.PAPER)))
        print(t.kv("attestation date", t.paint(att.as_of_date or "?", t.PAPER)))
        print(t.kv("extraction conf.",
                   t.paint(f"{att.confidence:.2f}", t.PAPER)))
    else:
        print(t.kv("attested reserves", f"{t.icon('warn')} "
                   f"{t.paint('unavailable', t.MUTED)}"))
    if a.metrics is not None:
        m = a.metrics
        print(t.kv("attested coverage", t.coverage_badge(m.attested_coverage)))
        print(t.kv("live coverage", t.coverage_badge(m.live_coverage)))
        print(t.kv("attestation age", t.paint(f"{m.staleness_days} days", t.PAPER)))
        drift = ("n/a" if m.supply_drift is None
                 else f"{m.supply_drift * 100:+.2f}%")
        print(t.kv("supply drift", t.paint(drift, t.PAPER)))
    print()

    # Guardrails — deterministic checks over the critical data
    if a.checks:
        passed = sum(1 for c in a.checks if c.passed)
        print(f"  {t.icon('info')} {t.paint('GUARDRAILS', t.PAPER, bold=True)}  "
              f"{t.paint(f'{passed}/{len(a.checks)} passed', t.MUTED)}")
        for c in a.checks:
            mark = "ok" if c.passed else ("error" if c.severity == "critical"
                                          else "warn")
            print(f"  {t.icon(mark)} {t.paint(c.name.ljust(26), t.PAPER)} "
                  f"{t.paint(c.detail, t.MUTED, dim=True)}")
        print()

    # Reasoning frame
    print(f"  {t.icon('corpus')} {t.paint('REASONING FRAME', t.PAPER, bold=True)}")
    if a.passages:
        for p in a.passages:
            print(f"  {t.icon('cite')} {t.paint(p.citation, t.GOLD)}  "
                  f"{t.paint(p.heading, t.MUTED)}")
    else:
        print(f"  {t.icon('warn')} {t.paint('no approved corpus passages', t.AMBER)}")
    print()

    # Narrative
    if a.narrative.strip():
        print(f"  {t.icon('agent')} {t.paint('ANALYSIS', t.PAPER, bold=True)}")
        for line in a.narrative.strip().splitlines():
            print(f"  {t.paint(line, t.PAPER)}")
        print()

    # Gaps
    print(f"  {t.icon('info')} {t.paint('CONFIDENCE & GAPS', t.PAPER, bold=True)}")
    if a.gaps:
        for gap in a.gaps:
            mark = "error" if gap.severity == "critical" else "warn"
            color = t.ROSE if gap.severity == "critical" else t.AMBER
            print(f"  {t.icon(mark)} {t.paint(f'[{gap.category}]', t.MUTED)} "
                  f"{t.paint(gap.message, color)}")
    else:
        print(f"  {t.icon('ok')} {t.paint('no gaps reported', t.GREEN)}")
    print()


def render_tokens() -> None:
    coins = config.stablecoins()
    print(f"  {t.icon('reserve')} {t.paint('CONFIGURED STABLECOINS', t.PAPER, bold=True)}"
          f"  {t.paint(f'{len(coins)} tokens', t.MUTED)}")
    print()
    for sym, coin in coins.items():
        verified = sum(1 for d in coin.deployments if d.verified)
        total = len(coin.deployments)
        state = "ok" if verified == total else "warn"
        print(f"  {t.icon(state)} {t.paint(sym.ljust(7), t.GOLD)} "
              f"{t.paint(coin.name.ljust(20), t.PAPER)} "
              f"{t.paint(f'{verified}/{total} addrs verified', t.MUTED)}")
    print()


def render_sources() -> None:
    sources = all_sources()
    approved = sum(1 for s in sources if s.approved)
    print(f"  {t.icon('corpus')} {t.paint('CORPUS SOURCES', t.PAPER, bold=True)}  "
          f"{t.paint(f'{approved}/{len(sources)} approved', t.MUTED)}")
    print()
    for s in sources:
        state = "ok" if s.approved else "warn"
        print(f"  {t.icon(state)} {t.paint(s.id.ljust(24), t.GOLD)} "
              f"{t.paint(s.tier.ljust(12), t.MUTED)} "
              f"{t.paint(s.status, t.GREEN if s.approved else t.AMBER)}")
    print()
    print(f"  {t.paint('Only approved sources are citeable. A human approves '
                        'in corpus/sources.yaml.', t.MUTED)}")
    print()


def render_evals() -> None:
    results = run_evals()
    passed = sum(1 for r in results if r.passed)
    print(f"  {t.icon('agent')} {t.paint('EVAL HARNESS', t.PAPER, bold=True)}  "
          f"{t.paint(f'{passed}/{len(results)} cases passed', t.MUTED)}")
    print()
    for r in results:
        state = "ok" if r.passed else "error"
        print(f"  {t.icon(state)} {t.paint(r.case_id.ljust(26), t.GOLD)} "
              f"{t.paint(r.symbol, t.MUTED)}")
        for pt in r.points:
            mark = "ok" if pt.passed else "error"
            detail = f"  {t.paint(pt.detail, t.MUTED, dim=True)}" if pt.detail else ""
            print(f"      {t.icon(mark)} {t.paint(pt.point, t.PAPER)}{detail}")
    print()


def cmd_curate() -> None:
    """Interactive voting — record human decisions to the ledger."""
    if not sys.stdin.isatty():
        print(f"  {t.icon('warn')} "
              f"{t.paint('curate is interactive — run it in a terminal', t.AMBER)}")
        return

    # Sources awaiting approval.
    proposed = [s for s in all_sources() if s.status == "proposed"]
    print(f"  {t.icon('corpus')} {t.paint('CORPUS SOURCES', t.PAPER, bold=True)}  "
          f"{t.paint(f'{len(proposed)} awaiting your vote', t.MUTED)}")
    print()
    for s in proposed:
        print(f"  {t.icon('cite')} {t.paint(s.id, t.GOLD)}  "
              f"{t.paint(s.tier, t.MUTED)}")
        print(f"    {t.paint(s.title, t.PAPER)}")
        choice = input(f"    {t.paint('[a]pprove  [r]eject  [s]kip >', t.GOLD)} "
                       ).strip().lower()
        if choice == "a":
            votes.record_source_decision(s.id, "approved")
            all_sources.cache_clear()
            staged = config.CORPUS_DIR / "staging" / f"{s.id}.md"
            if staged.exists():
                chunks = ingest_source(s.id, staged.read_text())
                print(f"    {t.icon('ok')} "
                      f"{t.paint(f'approved · ingested {len(chunks)} chunks', t.GREEN)}")
            else:
                print(f"    {t.icon('ok')} "
                      f"{t.paint('approved · no staged content to ingest', t.GREEN)}")
        elif choice == "r":
            votes.record_source_decision(s.id, "rejected")
            print(f"    {t.icon('error')} {t.paint('rejected', t.ROSE)}")
        else:
            print(f"    {t.paint('skipped', t.MUTED)}")
        print()

    # Unverified contract addresses.
    pending = [
        (sym, dep)
        for sym, coin in config.stablecoins().items()
        for dep in coin.deployments
        if not dep.verified
    ]
    print(f"  {t.icon('reserve')} {t.paint('CONTRACT ADDRESSES', t.PAPER, bold=True)}"
          f"  {t.paint(f'{len(pending)} unverified', t.MUTED)}")
    print()
    for sym, dep in pending:
        print(f"  {t.icon('warn')} {t.paint(f'{sym} · {dep.chain}', t.GOLD)}")
        print(f"    {t.paint(dep.contract, t.PAPER)}")
        choice = input(f"    {t.paint('[v]erified  [r]eject  [s]kip >', t.GOLD)} "
                       ).strip().lower()
        if choice == "v":
            votes.record_address_decision(sym, dep.chain, "verified")
            print(f"    {t.icon('ok')} {t.paint('marked verified', t.GREEN)}")
        elif choice == "r":
            votes.record_address_decision(sym, dep.chain, "rejected")
            print(f"    {t.icon('error')} {t.paint('rejected', t.ROSE)}")
        else:
            print(f"    {t.paint('skipped', t.MUTED)}")
        print()

    print(f"  {t.icon('ok')} {t.paint('votes recorded to votes.yaml', t.GREEN)}")
    print()


def cmd_refresh() -> None:
    """Re-resolve attestation URLs from issuer transparency pages."""
    coins = config.stablecoins()
    print(f"  {t.icon('doc')} {t.paint('ATTESTATION URL REFRESH', t.PAPER, bold=True)}")
    print()
    for sym, coin in coins.items():
        if not coin.transparency_url and not coin.latest_attestation_url:
            print(f"  {t.icon('bullet')} {t.paint(sym.ljust(7), t.MUTED)} "
                  f"{t.paint('no transparency_url — skipped', t.MUTED)}")
            continue
        try:
            out = resolve_url(sym, refresh=True)
            print(f"  {t.icon('ok')} {t.paint(sym.ljust(7), t.GOLD)} "
                  f"{t.paint('via ' + out['via'], t.MUTED)}")
            print(f"      {t.paint(out['url'], t.PAPER)}")
        except AttestationUnavailable as exc:
            print(f"  {t.icon('warn')} {t.paint(sym.ljust(7), t.GOLD)} "
                  f"{t.paint(str(exc), t.AMBER)}")
    print()
    print(f"  {t.paint('Resolved URLs cached to data/attestation_cache.json', t.MUTED)}")
    print()


def _render_checks(checks) -> None:
    if not checks:
        return
    passed = sum(1 for c in checks if c.passed)
    print(f"  {t.icon('info')} {t.paint('GUARDRAILS', t.PAPER, bold=True)}  "
          f"{t.paint(f'{passed}/{len(checks)} passed', t.MUTED)}")
    for c in checks:
        mark = ("ok" if c.passed
                else "error" if c.severity == "critical" else "warn")
        print(f"  {t.icon(mark)} {t.paint(c.name.ljust(28), t.PAPER)} "
              f"{t.paint(c.detail, t.MUTED, dim=True)}")
    print()


def _render_passages(passages) -> None:
    print(f"  {t.icon('corpus')} {t.paint('REASONING FRAME', t.PAPER, bold=True)}")
    if passages:
        for p in passages:
            print(f"  {t.icon('cite')} {t.paint(p.citation, t.GOLD)}  "
                  f"{t.paint(p.heading, t.MUTED)}")
    else:
        print(f"  {t.icon('warn')} "
              f"{t.paint('no approved corpus passages', t.AMBER)}")
    print()


def _render_narrative(narrative: str, label: str) -> None:
    if not narrative.strip():
        return
    print(f"  {t.icon('agent')} {t.paint(label, t.PAPER, bold=True)}")
    for line in narrative.strip().splitlines():
        print(f"  {t.paint(line, t.PAPER)}")
    print()


def _render_gaps(gaps) -> None:
    print(f"  {t.icon('info')} {t.paint('CONFIDENCE & GAPS', t.PAPER, bold=True)}")
    if gaps:
        for g in gaps:
            mark = "error" if g.severity == "critical" else "warn"
            color = t.ROSE if g.severity == "critical" else t.AMBER
            print(f"  {t.icon(mark)} {t.paint(f'[{g.category}]', t.MUTED)} "
                  f"{t.paint(g.message, color)}")
    else:
        print(f"  {t.icon('ok')} {t.paint('no gaps reported', t.GREEN)}")
    print()


def render_sanctions(s) -> None:
    print(f"  {t.heading('F5', 'sanctions screen')}"
          f"   {t.paint(s.symbol, t.GOLD, bold=True)}")
    print()
    print(f"  {t.icon('reserve')} {t.paint('OFAC SDN SCREEN', t.PAPER, bold=True)}")
    if s.hits:
        print(t.kv("result", f"{t.icon('error')} "
              f"{t.paint(f'{len(s.hits)} SANCTIONED MATCH', t.ROSE)}"))
    else:
        print(t.kv("result", f"{t.icon('ok')} {t.paint('clear', t.GREEN)}"))
    print(t.kv("addresses screened", t.paint(str(len(s.screened)), t.PAPER)))
    print(t.kv("SDN list published", t.paint(s.sdn_publish_date or "?", t.PAPER)))
    print(t.kv("SDN crypto addresses",
               t.paint(f"{s.sdn_address_count:,}", t.PAPER)))
    for h in s.hits:
        print(f"  {t.icon('error')} {t.paint(h.address, t.ROSE)}  "
              f"{t.paint(h.currency, t.MUTED)}  {t.paint(h.sdn_name, t.PAPER)}")
    print()
    _render_checks(s.checks)
    _render_passages(s.passages)
    _render_narrative(s.narrative, "SANCTIONS ANALYSIS")
    _render_gaps(s.gaps)


def render_redemption(a) -> None:
    print(f"  {t.heading('F6', 'redemption capacity')}"
          f"   {t.paint(a.symbol, t.GOLD, bold=True)}")
    print()
    print(f"  {t.icon('metric')} {t.paint('SNAPSHOT', t.PAPER, bold=True)}")
    print(t.kv("on-chain supply", t.paint(_money(a.supply.total_supply), t.PAPER)))
    if a.attestation is not None:
        print(t.kv("attested reserves",
                   t.paint(_money(a.attestation.total_reserves), t.PAPER)))
    print(t.kv("liquid reserves", t.paint(_money(a.liquid_reserves), t.PAPER)))
    print(t.kv("liquid coverage", t.coverage_badge(a.liquid_coverage)))
    if a.net_redemption_flow is not None:
        print(t.kv("net redemption flow",
                   t.paint(f"{a.net_redemption_flow:+,.0f}", t.PAPER)))
    print()
    if a.tiers:
        print(f"  {t.icon('reserve')} "
              f"{t.paint('RESERVE LIQUIDITY', t.PAPER, bold=True)}")
        tier_color = {"liquid": t.GREEN, "moderate": t.AMBER, "illiquid": t.ROSE}
        for tr in a.tiers:
            print(f"  {t.paint(tr.tier.ljust(9), tier_color.get(tr.tier, t.MUTED))} "
                  f"{t.paint(_money(tr.amount).rjust(20), t.PAPER)}  "
                  f"{t.paint(tr.asset_class, t.MUTED)}")
        print()
    _render_checks(a.checks)
    _render_passages(a.passages)
    _render_narrative(a.narrative, "REDEMPTION ANALYSIS")
    _render_gaps(a.gaps)


# ── entrypoint ────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sca", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "supply", "screen", "redemption"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
        p.add_argument(
            "--json", action="store_true",
            help="emit machine-readable JSON instead of the report",
        )
        p.add_argument(
            "--refresh", action="store_true",
            help="bypass the analysis cache and recompute from scratch",
        )
    sub.add_parser("tokens")
    sub.add_parser("sources")
    sub.add_parser("curate")
    sub.add_parser("refresh")
    sub.add_parser("evals")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze":
            result = run_analyze(args.symbol.upper(), refresh=args.refresh)
            if args.json:
                _emit_json(result)
            else:
                _header("attestation analysis")
                render_analysis(result)
        elif args.command == "supply":
            result = get_onchain_supply(
                args.symbol.upper(), allow_unverified=True
            )
            if args.json:
                _emit_json(result)
            else:
                _header("on-chain supply")
                render_supply(result)
        elif args.command == "tokens":
            _header("token registry")
            render_tokens()
        elif args.command == "sources":
            _header("corpus curation")
            render_sources()
        elif args.command == "curate":
            _header("curation · voting")
            cmd_curate()
        elif args.command == "refresh":
            _header("attestation url refresh")
            cmd_refresh()
        elif args.command == "screen":
            result = screen_token(args.symbol.upper(), refresh=args.refresh)
            if args.json:
                _emit_json(result)
            else:
                _header("sanctions screen")
                render_sanctions(result)
        elif args.command == "redemption":
            result = assess_redemption(
                args.symbol.upper(), refresh=args.refresh
            )
            if args.json:
                _emit_json(result)
            else:
                _header("redemption capacity")
                render_redemption(result)
        elif args.command == "evals":
            _header("eval harness")
            render_evals()
    except Exception as exc:  # noqa: BLE001 - surface cleanly to the user
        print(f"  {t.icon('error')} {t.paint(str(exc), t.ROSE)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
