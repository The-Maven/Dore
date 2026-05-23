"""sca — terminal interface, consultancy gold/dark theme.

  sca analyze SYMBOL    full attestation analysis
  sca supply SYMBOL     on-chain supply only
  sca tokens            list configured stablecoins
  sca sources           list corpus sources + inclusion status
  sca curate            vote: exclude/include/verify sources, verify addresses
  sca verify [SYMBOL]   auto-verify contracts via on-chain self-report
  sca refresh           re-resolve attestation URLs + run auto-verification
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
from sca.tools.address_verify import verify_all


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
        if c.verification_method.startswith("auto"):
            badge = t.paint("  ✓ auto", t.MUTED)
        elif c.verification_method == "human":
            badge = t.paint("  ✓ human", t.GREEN)
        else:
            badge = t.paint("  unverified", t.AMBER)
        print(f"  {vmark} {t.paint(c.chain.ljust(10), t.PAPER)} "
              f"{t.paint(_money(c.supply).rjust(20), t.MUTED)}  {kind}{badge}")
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
            badge = (t.paint('  ✦ human-verified', t.GREEN)
                     if p.source_verified
                     else t.paint('  · auto-included', t.MUTED))
            print(f"  {t.icon('cite')} {t.paint(p.citation, t.GOLD)}  "
                  f"{t.paint(p.heading, t.MUTED)}{badge}")
    else:
        print(f"  {t.icon('warn')} "
              f"{t.paint('no ingested corpus passages', t.AMBER)}")
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
        auto = sum(1 for d in coin.deployments
                   if d.verification_method.startswith("auto"))
        human = sum(1 for d in coin.deployments if d.verification_method == "human")
        total = len(coin.deployments)
        state = "ok" if verified == total else "warn"
        breakdown = (f"{verified}/{total} verified" +
                     (f" · {human} human · {auto} auto" if verified else ""))
        print(f"  {t.icon(state)} {t.paint(sym.ljust(7), t.GOLD)} "
              f"{t.paint(coin.name.ljust(20), t.PAPER)} "
              f"{t.paint(breakdown, t.MUTED)}")
    print()


def render_sources() -> None:
    sources = all_sources()
    included = sum(1 for s in sources if s.included)
    verified = sum(1 for s in sources if s.verified)
    print(f"  {t.icon('corpus')} {t.paint('CORPUS SOURCES', t.PAPER, bold=True)}  "
          f"{t.paint(f'{included}/{len(sources)} included · {verified} verified',
                     t.MUTED)}")
    print()
    for s in sources:
        state = "ok" if s.included else "error"
        badge = "  ✦ human-verified" if s.verified else ""
        print(f"  {t.icon(state)} {t.paint(s.id.ljust(24), t.GOLD)} "
              f"{t.paint(s.tier.ljust(12), t.MUTED)} "
              f"{t.paint(s.status, t.GREEN if s.included else t.ROSE)}"
              f"{t.paint(badge, t.GOLD)}")
    print()
    print(f"  {t.paint('Every source is citeable by default. A human opts one '
                        'out with `sca curate`.', t.MUTED)}")
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

    # Sources — included by default; a human opts one out or verifies it.
    sources = list(all_sources())
    print(f"  {t.icon('corpus')} {t.paint('CORPUS SOURCES', t.PAPER, bold=True)}  "
          f"{t.paint(f'{len(sources)} registered · included by default', t.MUTED)}")
    print()
    for s in sources:
        flag = (t.paint('EXCLUDED', t.ROSE) if s.excluded
                else t.paint('included', t.GREEN))
        if s.verified:
            flag += t.paint('  ✦ verified', t.GOLD)
        print(f"  {t.icon('cite')} {t.paint(s.id, t.GOLD)}  "
              f"{t.paint(s.tier, t.MUTED)}  {flag}")
        print(f"    {t.paint(s.title, t.PAPER)}")
        choice = input(
            f"    {t.paint('[x]clude  [i]nclude  [v]erify  [s]kip >', t.GOLD)} "
        ).strip().lower()
        if choice == "x":
            votes.record_source_decision(s.id, "excluded")
            print(f"    {t.icon('error')} "
                  f"{t.paint('excluded — opted out of the corpus', t.ROSE)}")
        elif choice in ("i", "v"):
            decision = "verified" if choice == "v" else "included"
            votes.record_source_decision(s.id, decision)
            all_sources.cache_clear()
            staged = config.CORPUS_DIR / "staging" / f"{s.id}.md"
            label = "verified" if choice == "v" else "included"
            if staged.exists():
                chunks = ingest_source(s.id, staged.read_text())
                print(f"    {t.icon('ok')} "
                      f"{t.paint(f'{label} · ingested {len(chunks)} chunks',
                                 t.GREEN)}")
            else:
                print(f"    {t.icon('ok')} "
                      f"{t.paint(f'{label} · no staged text to ingest', t.GREEN)}")
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


def cmd_verify(symbol: str | None = None) -> None:
    """Auto-verify contract addresses via on-chain self-report.

    Asks each contract its own `symbol()` and `decimals()` and clears the
    unverified flag on a clean match. Mismatches and unsupported chains
    stay flagged — the actual cases a human should look at.
    """
    label = f"all tokens" if symbol is None else symbol.upper()
    print(f"  {t.icon('reserve')} "
          f"{t.paint('ADDRESS VERIFICATION', t.PAPER, bold=True)}  "
          f"{t.paint(label, t.MUTED)}")
    print()
    summary = verify_all(symbol)

    for sym, chain, _contract, result in summary["checked"]:
        if result["verified"]:
            print(f"  {t.icon('ok')} {t.paint(f'{sym} · {chain}'.ljust(22), t.GOLD)} "
                  f"{t.paint('auto-verified', t.GREEN)}  "
                  f"{t.paint(result['detail'], t.MUTED, dim=True)}")
        elif result["signal"] == "mismatch":
            print(f"  {t.icon('error')} {t.paint(f'{sym} · {chain}'.ljust(22), t.GOLD)} "
                  f"{t.paint('MISMATCH', t.ROSE)}  "
                  f"{t.paint(result['detail'], t.AMBER)}")
        elif result["signal"] == "unsupported-chain":
            print(f"  {t.icon('bullet')} {t.paint(f'{sym} · {chain}'.ljust(22), t.MUTED)} "
                  f"{t.paint('unsupported · needs human', t.MUTED)}")
        else:  # error
            print(f"  {t.icon('warn')} {t.paint(f'{sym} · {chain}'.ljust(22), t.GOLD)} "
                  f"{t.paint(result['detail'], t.AMBER)}")

    print()
    print(f"  {t.paint('SUMMARY', t.GOLD)}")
    print(t.kv("auto-verified", t.paint(str(summary['auto_verified']), t.GREEN)))
    print(t.kv("mismatch (review)",
               t.paint(str(summary['mismatch']),
                       t.ROSE if summary['mismatch'] else t.MUTED)))
    print(t.kv("unsupported chain", t.paint(str(summary['unsupported']), t.MUTED)))
    print(t.kv("rpc error",
               t.paint(str(summary['errored']),
                       t.AMBER if summary['errored'] else t.MUTED)))
    print(t.kv("skipped (human decided)",
               t.paint(str(summary['skipped_human']), t.MUTED)))
    print()
    print(f"  {t.paint('Auto-verifications cached to '
                       'data/auto_verifications.json. '
                       'Human votes (sca curate) always win.', t.MUTED)}")
    print()


def cmd_discover() -> None:
    """Sweep every discovery surface; auto-register anything new.

    Designed for a nightly cron — runs three surfaces:
      - tier1_official HTML scrapers (OFAC, BIS, FSB, EUR-Lex MiCA,
        NYDFS, IAASB)
      - tier1_official RSS / Atom feed pollers (BIS, FSB, NYDFS press,
        Fed speeches, ECB digital-euro)
      - tier2_industry issuer + analytics blog scrapers (Circle, Paxos,
        Tether, Chainalysis, TRM Labs, Elliptic)

    All three feed the same `sync_discovered()` pipeline, so dedup
    (by body hash) and the ledger / staging / `included`-by-default
    registration path is identical regardless of surface. The existing
    `sync_staging()` picks new sources up on the next `retrieve()`.
    """
    from sca.discovery import POLLERS, sync_discovered
    from sca.discovery_rss import POLLERS as RSS_POLLERS
    from sca.discovery_blogs import POLLERS as BLOG_POLLERS

    report = sync_discovered()

    all_pollers = sorted(
        list(POLLERS) + list(RSS_POLLERS) + list(BLOG_POLLERS)
    )
    print(f"  {t.paint('DISCOVERY · ' + ' · '.join(all_pollers),
                       t.GOLD, bold=True)}")
    print()
    if report.new:
        print(f"  {t.paint('NEW', t.GREEN, bold=True)}  "
              f"{t.paint(f'{len(report.new)} sources registered', t.MUTED)}")
        for sid in report.new:
            print(f"    {t.icon('ok')} {t.paint(sid, t.GOLD)}")
        print()
    if report.revised:
        print(f"  {t.paint('REVISED', t.AMBER, bold=True)}  "
              f"{t.paint(f'{len(report.revised)} updated bodies', t.MUTED)}")
        for sid in report.revised:
            print(f"    {t.icon('warn')} {t.paint(sid, t.GOLD)}")
        print()
    if report.unchanged:
        print(f"  {t.paint('UNCHANGED', t.MUTED, bold=True)}  "
              f"{t.paint(f'{len(report.unchanged)} already known', t.MUTED)}")
        print()
    if report.errors:
        print(f"  {t.paint('ERRORS', t.ROSE, bold=True)}  "
              f"{t.paint(f'{len(report.errors)} failures', t.MUTED)}")
        for sid, err in report.errors.items():
            print(f"    {t.icon('error')} {t.paint(sid.ljust(40), t.GOLD)} "
                  f"{t.paint(err[:60], t.ROSE)}")
        print()

    print(f"  {t.paint('TOTAL', t.GOLD, bold=True)}")
    print(t.kv("new", t.paint(str(len(report.new)),
                              t.GREEN if report.new else t.MUTED)))
    print(t.kv("revised", t.paint(str(len(report.revised)),
                                  t.AMBER if report.revised else t.MUTED)))
    print(t.kv("unchanged", t.paint(str(len(report.unchanged)), t.MUTED)))
    print(t.kv("errors", t.paint(str(len(report.errors)),
                                 t.ROSE if report.errors else t.MUTED)))
    print()
    print(f"  {t.paint('New sources staged to corpus/staging/. They '
                       'become retrievable on next sync_staging().',
                       t.MUTED)}")
    print()


def cmd_canary() -> None:
    """Re-fetch every external source and report drift.

    Sweeps: per-chain RPC endpoints, OFAC SDN feeds, issuer transparency
    URLs, corpus source URLs. Persists fresh snapshots for the UI's
    "archived copy" fallback and flags broken/changed sources loud enough
    to actually fix.
    """
    from sca.canary import run_canary

    report = run_canary()

    by_kind: dict[str, list] = {}
    for c in report.checks:
        by_kind.setdefault(c.kind, []).append(c)

    for kind, items in sorted(by_kind.items()):
        live = sum(1 for c in items if c.status == "live")
        broken = sum(1 for c in items if c.status == "broken")
        snap = sum(1 for c in items if c.status == "snapshot")
        changed = sum(1 for c in items if c.sha256_changed)
        head = (
            f"{kind.upper()}  {live} live · {snap} archived · "
            f"{broken} broken · {changed} changed"
        )
        print(f"  {t.paint(head, t.GOLD, bold=True)}")
        for c in items:
            label = c.id[:48]
            if c.status == "live":
                tag = t.paint("live", t.GREEN)
                if c.sha256_changed:
                    tag += " " + t.paint("· content changed", t.AMBER)
                print(f"    {t.icon('ok')} {label.ljust(50)} {tag}")
            elif c.status == "snapshot":
                print(f"    {t.icon('warn')} {label.ljust(50)} "
                      f"{t.paint('archived copy only', t.AMBER)}  "
                      f"{t.paint(c.error[:60], t.MUTED)}")
            else:
                print(f"    {t.icon('error')} {label.ljust(50)} "
                      f"{t.paint('BROKEN', t.ROSE)}  "
                      f"{t.paint(c.error[:60], t.MUTED)}")
        print()

    print(f"  {t.paint('TOTAL', t.GOLD, bold=True)}")
    print(t.kv("checked", str(len(report.checks))))
    print(t.kv("live", t.paint(str(report.live), t.GREEN)))
    print(t.kv("archived (live broken)",
               t.paint(str(report.snapshot_only),
                       t.AMBER if report.snapshot_only else t.MUTED)))
    print(t.kv("broken (no fallback)",
               t.paint(str(report.broken),
                       t.ROSE if report.broken else t.MUTED)))
    print(t.kv("content changed", str(report.changed)))
    print()


def cmd_refresh() -> None:
    """Re-resolve attestation URLs from issuer transparency pages.

    Also runs the on-chain address-verification pass so a routine refresh
    keeps the auto-verification state current — addresses that pass the
    self-report check clear the unverified wall automatically.
    """
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
    # Verification pass — keep the auto-verified state in sync with reality.
    cmd_verify(None)


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
            badge = (t.paint('  ✦ human-verified', t.GREEN)
                     if p.source_verified
                     else t.paint('  · auto-included', t.MUTED))
            print(f"  {t.icon('cite')} {t.paint(p.citation, t.GOLD)}  "
                  f"{t.paint(p.heading, t.MUTED)}{badge}")
    else:
        print(f"  {t.icon('warn')} "
              f"{t.paint('no ingested corpus passages', t.AMBER)}")
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
    verify_parser = sub.add_parser(
        "verify",
        help="auto-verify contract addresses (all tokens, or one symbol)",
    )
    verify_parser.add_argument("symbol", nargs="?", default=None)
    sub.add_parser(
        "canary",
        help="re-fetch every external source we depend on; report drift",
    )
    sub.add_parser(
        "discover",
        help="sweep official-source pollers; auto-register new publications",
    )
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
        elif args.command == "verify":
            _header("address verification")
            cmd_verify(args.symbol)
        elif args.command == "canary":
            _header("source canary")
            cmd_canary()
        elif args.command == "discover":
            _header("source discovery · nightly sweep")
            cmd_discover()
    except Exception as exc:  # noqa: BLE001 - surface cleanly to the user
        print(f"  {t.icon('error')} {t.paint(str(exc), t.ROSE)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
