"""
market_mapper.py — maps OKX market identifiers to Polymarket asset keywords.

This is a static, explicit mapping for Phase 1.  It is intentionally simple
and fully testable without any I/O.  No strategy, no trading signals.

OKX market_id format
--------------------
All keys in ``_ASSET_MAP`` and all ``market_id`` values passed to this module
**must** be bare OKX instrument IDs, for example::

    "BTC-USDT"      ✓  correct
    "ETH-USDT"      ✓  correct
    "okx:BTC-USDT"  ✗  wrong — prefixed IDs are not in _ASSET_MAP and will
                              silently produce 0 lag records

The OKX WebSocket public feed delivers bare instrument IDs in the ``instId``
field with no source prefix.  If you construct a ``market_id`` from another
source, strip any prefix (e.g. ``"okx:"``) before passing it here.
"""
from __future__ import annotations

# Mapping: OKX instrument ID → (canonical asset name, Polymarket keyword list)
# Keys must be bare OKX instrument IDs (e.g. "BTC-USDT"), never prefixed
# (e.g. "okx:BTC-USDT").  A prefixed key will not match _ASSET_MAP and will
# silently produce 0 lag records.
# Keywords are matched case-insensitively against Polymarket snapshot symbols.
_ASSET_MAP: dict[str, tuple[str, list[str]]] = {
    "BTC-USDT": ("BTC", ["BTC", "bitcoin", "Bitcoin"]),
    "ETH-USDT": ("ETH", ["ETH", "ethereum", "Ethereum"]),
    "SOL-USDT": ("SOL", ["SOL", "solana", "Solana"]),
}


def asset_for_okx_market(market_id: str) -> str | None:
    """Return the canonical asset name for an OKX market_id, or None if unmapped.

    *market_id* must be a bare OKX instrument ID such as ``"BTC-USDT"``.
    Prefixed IDs (e.g. ``"okx:BTC-USDT"``) will not match and return None,
    which silently produces 0 lag records downstream.
    """
    entry = _ASSET_MAP.get(market_id)
    return entry[0] if entry else None


def keywords_for_asset(asset: str) -> list[str]:
    """Return the Polymarket keyword list for a canonical asset name.

    Returns an empty list if the asset has no mapping.
    """
    for _asset, kws in _ASSET_MAP.values():
        if _asset == asset:
            return list(kws)
    return []


def okx_market_ids() -> list[str]:
    """Return the list of OKX market IDs that have a Polymarket mapping."""
    return list(_ASSET_MAP.keys())


def snapshot_matches_asset(symbol: str | None, keywords: list[str]) -> bool:
    """Return True if *symbol* contains any of *keywords* (case-insensitive)."""
    if not symbol:
        return False
    s_lower = symbol.lower()
    return any(kw.lower() in s_lower for kw in keywords)
