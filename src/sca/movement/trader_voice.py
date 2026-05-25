"""The Discipline Trader's voice — LLM-narrated commentary.

Three layers of narrative, each aggressively cached:
  • Daily brief — once per UTC day. "What I'm watching today and why."
  • Trade narration — once per opened trade. "Why this trade, why now."
  • End-of-day reflection — once per UTC day at close. "What worked,
    what didn't, what I'd do differently."

All three rely on the existing sca.llm client. Caches live on disk in
data/trader_voice/ so the cost of a missing call is one prompt, not
one prompt per pageload.

The LLM is treated as a writer, not a decider. The engine decides what
to trade; this layer just narrates. If the LLM call fails, the engine
keeps running — narration falls back to a deterministic template so
the receipt still has SOMETHING to read.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sca.config import DATA_DIR
from sca.observability import log_event


VOICE_DIR: Path = DATA_DIR / "trader_voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

_VOICE_LOCK = threading.Lock()


# ── shared LLM call ────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are The Discipline Trader — a deterministic stablecoin "
    "trader's narrator. Write in plain, professional English the "
    "way a senior desk-runner explains decisions to a CIO who is "
    "checking in mid-day. No hype, no jargon people outside finance "
    "wouldn't recognise, no apologies. Cite the specific numbers in "
    "the data provided; never invent figures. Keep it tight."
)


def _llm_complete(prompt: str, *, max_tokens: int = 350) -> Optional[str]:
    """Return LLM output or None on failure. Never raises — voice
    layer must not break the trader."""
    try:
        from sca.llm import get_llm
        client = get_llm(tier="fast")
        text = client.complete(
            system=_SYSTEM_PROMPT, prompt=prompt, max_tokens=max_tokens,
        )
        return (text or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        log_event(
            "trader_voice.llm_failed", level="warn",
            error_class=type(exc).__name__,
        )
        return None


# ── 1) Daily brief ─────────────────────────────────────────────────
@dataclass
class DailyBrief:
    day_utc: str                # YYYY-MM-DD
    generated_at: str
    body: str                   # the narrative
    fallback: bool = False      # True when the LLM call failed
    inputs_hash: str = ""       # input fingerprint for cache invalidation


def _brief_path(day_utc: str) -> Path:
    return VOICE_DIR / f"brief_{day_utc}.json"


def _fingerprint(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def generate_daily_brief(
    *,
    day_utc: str,
    token_states: list[dict],
    recent_news: list[dict],
    yesterday_pnl_usd: Optional[float] = None,
    force_refresh: bool = False,
) -> DailyBrief:
    """Generate (or load cached) the morning brief. Inputs:
      • token_states — current peg + cone snapshot for each token
      • recent_news  — top news items from brave_context
      • yesterday_pnl_usd — context anchor

    Cached by UTC day; once written, it stays. Force-refresh only
    when an operator explicitly asks for it (e.g. dramatic news
    drops mid-day)."""
    inputs = {
        "token_states": token_states,
        "recent_news": recent_news,
        "yesterday_pnl_usd": yesterday_pnl_usd,
    }
    fp = _fingerprint(inputs)
    path = _brief_path(day_utc)
    with _VOICE_LOCK:
        if path.exists() and not force_refresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("inputs_hash") == fp:
                    return DailyBrief(**cached)
            except (OSError, json.JSONDecodeError):
                pass

    # Build the prompt. Keep it FACTUAL — give the LLM the data, ask
    # it for a narrative read, not a forecast.
    lines = ["Today is " + day_utc + " UTC. Token states (live snapshot):"]
    for t in token_states[:18]:
        sym = t.get("symbol")
        bps = t.get("current_bps")
        cone = t.get("cone_half_bps")
        if bps is None:
            continue
        cone_str = f" ±{cone:.1f}bp cone" if cone else ""
        lines.append(
            f"  • {sym}: {bps:+.1f}bp from peg{cone_str}"
        )
    if recent_news:
        lines.append("\nRecent market context (web search):")
        for n in recent_news[:6]:
            title = (n.get("title") or "").strip()
            src = (n.get("source") or n.get("host") or "").strip()
            if title:
                tag = f" — {src}" if src else ""
                lines.append(f"  • {title}{tag}")
    if yesterday_pnl_usd is not None:
        sign = "+" if yesterday_pnl_usd >= 0 else ""
        lines.append(
            f"\nYesterday's P&L: {sign}${yesterday_pnl_usd:,.2f}."
        )
    lines.append(
        "\nWrite the morning brief: 3–4 sentences. Lead with what "
        "you're WATCHING today and WHY (anchor to specific tokens + "
        "their deviations + relevant news). Do not predict — observe."
    )

    body = _llm_complete("\n".join(lines), max_tokens=350)
    fallback = body is None
    if fallback:
        # Deterministic fallback so the UI still has something to read.
        biggest = sorted(
            (t for t in token_states if t.get("current_bps") is not None),
            key=lambda t: abs(t["current_bps"]), reverse=True,
        )[:3]
        if biggest:
            top = ", ".join(
                f"{t['symbol']} {t['current_bps']:+.1f}bp" for t in biggest
            )
            body = (
                f"{day_utc}: watching {top} for setup quality today. "
                f"Engine narration unavailable — falling back to "
                f"deterministic summary."
            )
        else:
            body = (
                f"{day_utc}: no token states available; engine standing by."
            )

    brief = DailyBrief(
        day_utc=day_utc,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        body=body,
        fallback=fallback,
        inputs_hash=fp,
    )
    with _VOICE_LOCK:
        try:
            path.write_text(json.dumps(asdict(brief), indent=2),
                            encoding="utf-8")
        except OSError as exc:
            log_event(
                "trader_voice.cache_write_failed", level="warn",
                kind="brief", error_class=type(exc).__name__,
            )
    return brief


def get_daily_brief(day_utc: str) -> Optional[DailyBrief]:
    """Return cached brief without generating. None if not yet
    generated."""
    path = _brief_path(day_utc)
    if not path.exists():
        return None
    try:
        return DailyBrief(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


# ── 2) Per-trade narration ─────────────────────────────────────────
def _narration_path(trade_id: str) -> Path:
    return VOICE_DIR / f"narration_{trade_id}.json"


def generate_trade_narration(trade: dict) -> str:
    """One-shot narration generated at trade-open. Cached forever by
    trade id — every trade gets ONE narration, regenerated only if
    the cache file is deleted.

    Falls back to a templated rationale on LLM failure."""
    tid = trade.get("id") or ""
    if tid:
        path = _narration_path(tid)
        with _VOICE_LOCK:
            if path.exists():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                    return cached.get("body") or ""
                except (OSError, json.JSONDecodeError):
                    pass

    # Build the prompt
    sym = trade.get("symbol", "?")
    direction = trade.get("direction", "?")
    notional = trade.get("notional_usd", 0)
    entry_bps = trade.get("entry_bps", 0)
    edge_bps = trade.get("edge_bps", 0)
    conviction = trade.get("conviction", "?")
    strategy = trade.get("strategy", "?")
    consensus_kind = trade.get("entry_consensus_kind", "")
    n_sources = len(trade.get("entry_sources") or [])
    rationale = trade.get("rationale", "")
    news = trade.get("entry_news_context") or []

    news_str = ""
    if news:
        news_str = "\nRecent news context:\n" + "\n".join(
            f"  • {n.get('title', '')}" for n in news[:3]
        )

    prompt = (
        f"Trade just opened on the live trading sim:\n"
        f"  Symbol: {sym}\n"
        f"  Direction: {direction.upper()}\n"
        f"  Notional: ${notional:,.0f}\n"
        f"  Conviction tier: {conviction}\n"
        f"  Strategy: {strategy}\n"
        f"  Entry deviation from peg: {entry_bps:+.2f}bp\n"
        f"  Predicted edge: {edge_bps:.2f}bp\n"
        f"  Consensus: {consensus_kind} across {n_sources} sources\n"
        f"  Deterministic rationale: {rationale}"
        f"{news_str}\n\n"
        f"Write the trader's narration: 1–2 sentences. Lead with "
        f"the specific setup that made this a {conviction}-tier "
        f"trade. Cite the numbers above; do not invent any. "
        f"Professional desk-runner voice."
    )

    body = _llm_complete(prompt, max_tokens=180)
    if not body:
        # Fallback: tier + size + edge in one line
        body = (
            f"{conviction} conviction: {direction} {sym} at "
            f"{entry_bps:+.2f}bp on {edge_bps:.1f}bp expected edge "
            f"across {n_sources} {consensus_kind} sources. "
            f"${notional:,.0f} on {strategy}."
        )

    if tid:
        with _VOICE_LOCK:
            try:
                _narration_path(tid).write_text(
                    json.dumps({"body": body, "generated_at":
                                 datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8")
            except OSError:
                pass
    return body


# ── 3) End-of-day reflection ───────────────────────────────────────
@dataclass
class DailyReflection:
    day_utc: str
    generated_at: str
    body: str
    inputs_hash: str = ""
    fallback: bool = False
    stats: dict = field(default_factory=dict)


def _reflection_path(day_utc: str) -> Path:
    return VOICE_DIR / f"reflection_{day_utc}.json"


def generate_reflection(
    *,
    day_utc: str,
    trades_today: list[dict],
    force_refresh: bool = False,
) -> DailyReflection:
    """Generate (or load cached) the end-of-day reflection. Pure
    post-mortem — what worked, what didn't, attribution by strategy
    and conviction tier."""
    resolved = [t for t in trades_today if t.get("status") == "resolved"
                 and t.get("outcome") != "STALE"]
    inputs = {"day": day_utc, "n_trades": len(resolved)}
    fp = _fingerprint({"trades": [t.get("id") for t in resolved]})
    path = _reflection_path(day_utc)
    with _VOICE_LOCK:
        if path.exists() and not force_refresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("inputs_hash") == fp:
                    return DailyReflection(**cached)
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    # Aggregate stats deterministically (the LLM gets ALREADY-computed
    # numbers — never asks it to do arithmetic on raw trade rows).
    total_pnl = sum(t.get("pnl_usd") or 0 for t in resolved)
    wins = [t for t in resolved if t.get("outcome") == "WIN"]
    losses = [t for t in resolved if t.get("outcome") == "LOSS"]
    by_strategy: dict = {}
    by_conviction: dict = {}
    for t in resolved:
        strat = t.get("strategy", "?")
        conv = t.get("conviction") or "?"
        pnl = t.get("pnl_usd") or 0
        by_strategy.setdefault(strat, {"n": 0, "pnl": 0.0})
        by_strategy[strat]["n"] += 1
        by_strategy[strat]["pnl"] += pnl
        by_conviction.setdefault(conv, {"n": 0, "pnl": 0.0})
        by_conviction[conv]["n"] += 1
        by_conviction[conv]["pnl"] += pnl

    stats = {
        "n_trades": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl_usd": round(total_pnl, 2),
        "by_strategy": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)}
                          for k, v in by_strategy.items()},
        "by_conviction": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)}
                            for k, v in by_conviction.items()},
    }

    if not resolved:
        body = (
            f"{day_utc}: no resolved trades today. The engine was "
            f"quiet because nothing cleared the conviction threshold "
            f"— that's the desired behaviour, not a bug."
        )
        return DailyReflection(
            day_utc=day_utc,
            generated_at=datetime.now(timezone.utc).isoformat(),
            body=body, stats=stats, inputs_hash=fp,
        )

    # Build LLM prompt
    lines = [f"End-of-day reflection for {day_utc} UTC."]
    lines.append(
        f"\nResolved trades: {len(resolved)} | wins {len(wins)} | "
        f"losses {len(losses)} | net P&L ${total_pnl:+,.2f}"
    )
    lines.append("\nBy strategy:")
    for strat, agg in by_strategy.items():
        lines.append(
            f"  • {strat}: {agg['n']} trades, ${agg['pnl']:+,.2f}")
    lines.append("\nBy conviction tier:")
    for conv, agg in by_conviction.items():
        lines.append(
            f"  • {conv}: {agg['n']} trades, ${agg['pnl']:+,.2f}")
    lines.append(
        "\nTop 5 trades by absolute P&L:")
    sorted_t = sorted(resolved, key=lambda t: abs(t.get("pnl_usd") or 0),
                       reverse=True)[:5]
    for t in sorted_t:
        sym = t.get("symbol", "?")
        pnl = t.get("pnl_usd") or 0
        outcome = t.get("outcome", "?")
        strat = t.get("strategy", "?")
        lines.append(
            f"  • {sym} {strat}: {outcome} ${pnl:+,.2f}")
    lines.append(
        "\nWrite a 3–4 sentence end-of-day reflection: what worked, "
        "what didn't, and ONE concrete observation about the desk's "
        "behaviour today (e.g. concentration in a strategy, missed "
        "setups, conviction tier hit-rate). Cite the numbers above. "
        "Professional, no apologies."
    )

    body = _llm_complete("\n".join(lines), max_tokens=320)
    fallback = body is None
    if fallback:
        body = (
            f"{day_utc}: {len(resolved)} trades, "
            f"{len(wins)}W/{len(losses)}L, net ${total_pnl:+,.2f}. "
            f"Engine narration unavailable; figures verified."
        )

    refl = DailyReflection(
        day_utc=day_utc,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        body=body,
        stats=stats,
        fallback=fallback,
        inputs_hash=fp,
    )
    with _VOICE_LOCK:
        try:
            path.write_text(json.dumps(asdict(refl), indent=2),
                            encoding="utf-8")
        except OSError as exc:
            log_event(
                "trader_voice.cache_write_failed", level="warn",
                kind="reflection", error_class=type(exc).__name__,
            )
    return refl


def get_reflection(day_utc: str) -> Optional[DailyReflection]:
    path = _reflection_path(day_utc)
    if not path.exists():
        return None
    try:
        return DailyReflection(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
