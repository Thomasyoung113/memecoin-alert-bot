"""
Wide scanner — monitors for Solana tokens hitting $10M+ MCap
and profiles their early buyers for smart money detection.
"""
import logging
import time
import threading

import requests

from config import DEXSCREENER_BASE, WIDE_SCAN_INTERVAL, WIDE_SCAN_MCAP_THRESHOLD
from bot.models import execute, commit, close_cursor

logger = logging.getLogger(__name__)

# Track tokens we've already profiled
_seen_tokens: set = set()


def _load_seen():
    c = execute("SELECT DISTINCT token_address FROM wallet_buys")
    rows = c.fetchall()
    close_cursor(c)
    for r in rows:
        _seen_tokens.add(r["token_address"])


def _fetch_token_profile_by_address(token_address: str) -> dict | None:
    """Fetch DexScreener pair data for a specific token."""
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        for p in pairs:
            if p.get("chainId") == "solana":
                return p
    except requests.RequestException as e:
        logger.debug("Wide scan: failed to fetch %s: %s", token_address[:8], e)
    return None


def _find_tokens_near_threshold() -> list[str]:
    """
    Check DexScreener token profiles for tokens that might be
    approaching $10M MCap. Returns token addresses.
    """
    url = f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        profiles = resp.json()
    except requests.RequestException:
        return []

    candidates = []
    for p in profiles:
        if p.get("chainId") != "solana":
            continue
        addr = p.get("tokenAddress", "")
        if addr and addr not in _seen_tokens:
            candidates.append(addr)

    return candidates


def get_mcap_for_token(token_address: str) -> float | None:
    """Get current market cap for a token. Returns None if not found."""
    pair = _fetch_token_profile_by_address(token_address)
    if pair:
        return pair.get("marketCap") or 0
    return None


def profile_token(token_address: str) -> list[str]:
    """
    Full profile of a token: get early buyers and save to DB.

    Returns list of early buyer wallet addresses found.
    """
    from bot.helius import get_early_buyers
    from config import FIRST_BUYERS_DEPTH

    _seen_tokens.add(token_address)

    logger.info("Profiling token %s for smart money...", token_address[:8])
    buyers = get_early_buyers(token_address, depth=FIRST_BUYERS_DEPTH)

    if not buyers:
        logger.debug("No early buyers found for %s", token_address[:8])
        return []

    # Save to DB
    for i, wallet in enumerate(buyers):
        c = execute(
            "INSERT INTO wallets (address, first_seen) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (wallet, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        )
        close_cursor(c)
        c = execute("""
            INSERT INTO wallet_buys
                (wallet_address, token_address, buy_position, detected_at)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (wallet, token_address, i + 1,
              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        close_cursor(c)
        c = execute("""
            UPDATE wallets SET total_early_buys = total_early_buys + 1
            WHERE address = %s
        """, (wallet,))
        close_cursor(c)
    commit()

    logger.info("Saved %d early buyers for %s", len(buyers), token_address[:8])
    return buyers


def wide_scan_cycle():
    """
    One wide scan cycle:
    1. Check token profiles for new tokens
    2. For each, fetch MCap
    3. If MCap >= $10M, profile the token's early buyers
    """
    if not _seen_tokens:
        _load_seen()

    candidates = _find_tokens_near_threshold()
    profiled = 0

    for addr in candidates:
        if addr in _seen_tokens:
            continue

        mcap = get_mcap_for_token(addr)
        if mcap is None:
            continue

        logger.debug("Wide scan: %s MCap=$%.0f", addr[:8], mcap)

        if mcap >= WIDE_SCAN_MCAP_THRESHOLD:
            buyers = profile_token(addr)
            if buyers:
                profiled += 1
                logger.info("✅ Profiled %s ($%.0f MCap) — %d early buyers",
                            addr[:8], mcap, len(buyers))

        # Small delay between checks
        time.sleep(0.2)

    return profiled


def start_wide_scanner():
    """
    Start the wide scanner in a background daemon thread.
    Runs every WIDE_SCAN_INTERVAL seconds.
    """
    def _loop():
        logger.info("Wide scanner started (interval: %ds, threshold: $%.0f)",
                     WIDE_SCAN_INTERVAL, WIDE_SCAN_MCAP_THRESHOLD)
        while True:
            try:
                profiled = wide_scan_cycle()
                if profiled:
                    logger.info("Wide scan: profiled %d tokens this cycle",
                                profiled)
            except Exception as e:
                logger.exception("Wide scan error: %s", e)
            time.sleep(WIDE_SCAN_INTERVAL)

    thread = threading.Thread(target=_loop, daemon=True, name="wide-scanner")
    thread.start()
    logger.info("Wide scanner thread started")
    return thread