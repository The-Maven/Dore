"""Brand colour per stablecoin ticker.

Every UI surface that renders a token should pull its accent colour
from here so a USDC chip, USDC sparkline, and USDC candle share the
same hue. Avoids the "every dashboard picks a different blue for
USDC" mess.

Colours are chosen for **dark-theme legibility** first. Where the
issuer's official brand colour falls flat against an #0A0B0E
background (Circle's #2775CA reads muddy on dark), the entry uses
a tuned variant that preserves brand identity while staying
high-contrast. The original brand hex is preserved in the comment
for audit.

Honest fallback: tokens not in the registry get a deterministic-
but-anonymous colour from a fallback palette. The fallback IS
honest (no fake brand attribution), and the UI surfaces show an
"unbranded" badge on tokens that fall through.
"""
from __future__ import annotations

# Primary brand colours. Each entry is (accent_hex, glow_alpha_hex).
# The glow is used for the live-pulse halo behind the symbol.
# Dark-theme variants tuned for WCAG AA against #0A0B0E. Brand
# hex preserved in the comment for audit; the `accent` is the
# rendered colour. Research source: external SF design audit.
TOKEN_BRAND_COLOURS: dict[str, dict[str, str]] = {
    # Major fiat-backed
    "USDC":  {"accent": "#4F9DFF", "glow": "#4F9DFF66", "name": "Circle"},        # brand #2775CA
    "USDT":  {"accent": "#3FCFA2", "glow": "#3FCFA266", "name": "Tether"},        # brand #26A17B
    "DAI":   {"accent": "#F5AC37", "glow": "#F5AC3766", "name": "Sky"},           # brand keeps
    "PYUSD": {"accent": "#5B7BFF", "glow": "#5B7BFF66", "name": "PayPal"},        # brand #003087
    "USDP":  {"accent": "#43C97E", "glow": "#43C97E66", "name": "Paxos"},        # brand #0E794D
    "TUSD":  {"accent": "#36D896", "glow": "#36D89666", "name": "TrueUSD"},      # brand #1AB67E
    "FDUSD": {"accent": "#5BB8FF", "glow": "#5BB8FF66", "name": "First Digital"}, # brand #0066CC
    "USDG":  {"accent": "#A6A8FF", "glow": "#A6A8FF66", "name": "Global Dollar"},
    "GUSD":  {"accent": "#00DCFA", "glow": "#00DCFA66", "name": "Gemini"},       # brand keeps
    "AEUR":  {"accent": "#FFC107", "glow": "#FFC10766", "name": "Anchored EUR"},
    "EURI":  {"accent": "#FF6E40", "glow": "#FF6E4066", "name": "Eurite"},
    "EURC":  {"accent": "#7BB0E8", "glow": "#7BB0E866", "name": "Circle EUR"},  # USDC-blue desat for €

    # Crypto-collateralized
    "FRAX":   {"accent": "#E8E4D8", "glow": "#E8E4D866", "name": "Frax"},        # newsprint mono
    "LUSD":   {"accent": "#9B86FF", "glow": "#9B86FF66", "name": "Liquity"},     # brand #745DDF
    "GHO":    {"accent": "#D67DC4", "glow": "#D67DC466", "name": "Aave"},        # brand #B6509E
    "crvUSD": {"accent": "#FF7373", "glow": "#FF737366", "name": "Curve"},       # brand #9B1B1B
    "AUSD":   {"accent": "#7E94B8", "glow": "#7E94B866", "name": "Agora"},       # slate

    # Synthetic / new-or-unverified
    "USDe":  {"accent": "#A6A8FF", "glow": "#A6A8FF66", "name": "Ethena"},       # lavender-tech
    "USDf":  {"accent": "#FF8FA3", "glow": "#FF8FA366", "name": "Falcon"},
    "USDD":  {"accent": "#1CB854", "glow": "#1CB85466", "name": "TRON DAO"},
    "USDX":  {"accent": "#EF6C00", "glow": "#EF6C0066", "name": "Stables Labs"},
    "USDY":  {"accent": "#5C7AEA", "glow": "#5C7AEA66", "name": "Ondo"},
    "M":     {"accent": "#A78BFA", "glow": "#A78BFA66", "name": "M^0"},
}

# Anonymous fallback palette: distinct hues that don't claim brand
# attribution. Used for any ticker not in the registry. The "ix"
# field selects a stable colour by symbol hash so the same unknown
# token always gets the same colour across renders.
_FALLBACK_PALETTE = [
    "#C68A8F", "#92C180", "#A7C8E0", "#C6A8D4",
    "#E0BC7A", "#88B0B0", "#D49D6A", "#B0C4B1",
]


def brand_for(symbol: str) -> dict[str, str]:
    """Return {accent, glow, name, branded} for `symbol`. `branded`
    is True when the entry came from the registry; False when we
    fell back to an anonymous slot."""
    sym = (symbol or "").strip()
    sym_u = sym.upper()
    # Try exact case first (crvUSD's mixed case matters) then upper.
    for key in (sym, sym_u):
        if key in TOKEN_BRAND_COLOURS:
            row = TOKEN_BRAND_COLOURS[key]
            return {
                "accent": row["accent"],
                "glow": row["glow"],
                "name": row.get("name", ""),
                "branded": True,
            }
    # Anonymous fallback.
    h = 0
    for ch in sym_u:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    idx = h % len(_FALLBACK_PALETTE)
    return {
        "accent": _FALLBACK_PALETTE[idx],
        "glow": _FALLBACK_PALETTE[idx] + "66",
        "name": "",
        "branded": False,
    }


def all_branded() -> dict[str, dict[str, str]]:
    """Return the full registry as plain dicts — useful for the
    /api/simulator/feed payload so the client can render without
    hardcoding any colours of its own."""
    return {sym: brand_for(sym) for sym in TOKEN_BRAND_COLOURS}
