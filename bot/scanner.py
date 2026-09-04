"""
DexScreener scanner — polls for new Solana tokens and runs the filter pipeline.
"""
import time
import json
import logging
from datetime import datetime, timezone

import requests

from config import (
    DEXSCREENER_BASE, POLL_INTERVAL,
    MCAP_MIN, MCAP_MAX, MAX_AGE_HOURS,
    BUYS_24H_MIN, SELLS_24H_MIN,
    TXS_5M_MIN, VOL_5M_MIN,
    LIQUIDITY_MIN, TWO_X_TARGET,
    FILTERS_TO_TUNE,
)
from bot.models import (
    get_filter_config, get_all_filter_configs, execute, close_cursor,
)

logger = logging.getLogger(__name__)

# Track token addresses we've already seen to avoid re-processing
_seen_tokens: set = set()


def _load_seen_tokens():
    c = execute("SELECT token_address FROM alerts")
    rows = c.fetchall()
    close_cursor(c)
    _seen_tokens.update(r[0] for r in rows)


def _fetch_token_profiles() -> list[dict]:
    """Fetch the latest token profiles from DexScreener."""
    url = f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_pair_data(token_address: str) -> dict | None:
    """Fetch full pair data for a token. Returns the first Solana pair, or None."""
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    pairs = data.get("pairs", [])
    # We only care about Solana pairs
    for p in pairs:
        if p.get("chainId") == "solana":
            return p
    return None


def _hours_since(created_epoch_ms: int) -> float:
    """Calculate how many hours ago a token was created."""
    created = datetime.fromtimestamp(created_epoch_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() / 3600


def _resolve_filter(name: str, default_value) -> float:
    """Get a filter value from the DB (set by learner), falling back to config."""
    db_val = get_filter_config(name)
    if db_val is not None:
        try:
            return float(db_val)
        except (ValueError, TypeError):
            pass
    return float(default_value)


def _check_filters(pair: dict) -> tuple[bool, dict]:
    """Run the filter pipeline on a pair. Returns (passed, snapshot)."""
    txns = pair.get("txns", {})
    volume = pair.get("volume", {})
    liquidity = pair.get("liquidity", {})
    pair_created = pair.get("pairCreatedAt", 0)

    age_hours = _hours_since(pair_created) if pair_created else 999

    # Resolve dynamic filter values (may have been tuned by learner)
    mcap_min = _resolve_filter("mcap_min", MCAP_MIN)
    mcap_max = _resolve_filter("mcap_max", MCAP_MAX)
    vol_5m_min = _resolve_filter("vol_5m_min", VOL_5M_MIN)
    buys_24h_min = _resolve_filter("buys_24h_min", BUYS_24H_MIN)
    sells_24h_min = _resolve_filter("sells_24h_min", SELLS_24H_MIN)
    txs_5m_min = _resolve_filter("txs_5m_min", TXS_5M_MIN)

    market_cap = (pair.get("marketCap") or 0)
    liq_usd = (liquidity.get("usd") or 0)
    vol_5m = (volume.get("m5") or 0)
    buys_24h = (txns.get("h24", {}).get("buys") or 0)
    sells_24h = (txns.get("h24", {}).get("sells") or 0)
    buys_5m = (txns.get("m5", {}).get("buys") or 0)
    sells_5m = (txns.get("m5", {}).get("sells") or 0)
    txs_5m = buys_5m + sells_5m

    # Snapshot for recording
    snapshot = {
        "mcap": market_cap,
        "liquidity_usd": liq_usd,
        "vol_5m": vol_5m,
        "age_hours": round(age_hours, 1),
        "buys_24h": buys_24h,
        "sells_24h": sells_24h,
        "txs_5m": txs_5m,
        "price_usd": pair.get("priceUsd"),
        "price_change_m5": pair.get("priceChange", {}).get("m5"),
        "price_change_h1": pair.get("priceChange", {}).get("h1"),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "fdv": pair.get("fdv"),
    }

    # ── Filter checks ──
    if liq_usd < LIQUIDITY_MIN:
        logger.debug("FAIL liq %.0f < %d", liq_usd, LIQUIDITY_MIN)
        return False, snapshot
    if not (mcap_min <= market_cap <= mcap_max):
        logger.debug("FAIL mcap %.0f not in [%.0f, %.0f]",
                     market_cap, mcap_min, mcap_max)
        return False, snapshot
    if age_hours > MAX_AGE_HOURS:
        logger.debug("FAIL age %.1f > %d", age_hours, MAX_AGE_HOURS)
        return False, snapshot
    if buys_24h < buys_24h_min:
        logger.debug("FAIL buys_24h %d < %.0f", buys_24h, buys_24h_min)
        return False, snapshot
    if sells_24h < sells_24h_min:
        logger.debug("FAIL sells_24h %d < %.0f", sells_24h, sells_24h_min)
        return False, snapshot
    if txs_5m < txs_5m_min:
        logger.debug("FAIL txs_5m %d < %.0f", txs_5m, txs_5m_min)
        return False, snapshot
    if vol_5m < vol_5m_min:
        logger.debug("FAIL vol_5m %.0f < %.0f", vol_5m, vol_5m_min)
        return False, snapshot

    logger.info(
        "PASS %s (MCap=$%.0f, Liq=$%.0f, Vol5m=$%.0f, Age=%.1fh)",
        pair.get("baseToken", {}).get("symbol", "?"),
        market_cap, liq_usd, vol_5m, age_hours,
    )
    return True, snapshot


def scan() -> list[tuple]:
    """
    Scan DexScreener for new Solana tokens that pass the filter pipeline.

    Returns: list of (token_address, symbol, mcap, price, snapshot, pair)
    """
    if not _seen_tokens:
        _load_seen_tokens()

    results = []

    try:
        profiles = _fetch_token_profiles()
    except requests.RequestException as e:
        logger.warning("Failed to fetch token profiles: %s", e)
        return results

    for profile in profiles:
        if profile.get("chainId") != "solana":
            continue

        token_address = profile.get("tokenAddress", "")
        if not token_address or token_address in _seen_tokens:
            continue

        # Mark as seen immediately to avoid duplicate processing
        _seen_tokens.add(token_address)

        try:
            pair = _fetch_pair_data(token_address)
        except requests.RequestException as e:
            logger.debug("Failed to fetch pair data for %s: %s",
                         token_address[:8], e)
            continue

        if pair is None:
            continue

        passed, snapshot = _check_filters(pair)
        if not passed:
            continue

        base_token = pair.get("baseToken", {})
        symbol = base_token.get("symbol", "?")
        mcap = snapshot["mcap"]
        price = float(pair.get("priceUsd") or 0)

        # 6th element: the pair itself — wash detector scores the SAME
        # snapshot the filters used (no second DexScreener fetch).
        results.append((token_address, symbol, mcap, price, snapshot, pair))

        # Small delay to avoid hammering the API
        time.sleep(0.1)

    return results