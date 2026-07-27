"""
Backtest — run the filter pipeline against historical token data to
evaluate filter effectiveness.

Usage:
    cd /root/alert-bot && python3 -c 'from bot.backtest import run_backtest; run_backtest()'
"""
import logging
import time
from datetime import datetime, timezone

import requests

from config import (
    DEXSCREENER_BASE,
    LIQUIDITY_MIN, MCAP_MIN, MCAP_MAX, MAX_AGE_HOURS,
    BUYS_24H_MIN, SELLS_24H_MIN, TXS_5M_MIN, VOL_5M_MIN,
)
from bot.models import get_conn

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────

def _hours_since(created_epoch_ms: int) -> float:
    """Calculate how many hours ago a token was created."""
    created = datetime.fromtimestamp(created_epoch_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() / 3600


def _extract_metrics(pair: dict) -> dict:
    """Extract standardised metrics dict from a DexScreener pair."""
    txns = pair.get("txns", {})
    volume = pair.get("volume", {})
    liquidity = pair.get("liquidity", {})
    price_change = pair.get("priceChange", {})
    pair_created = pair.get("pairCreatedAt", 0)

    age_hours = _hours_since(pair_created) if pair_created else 999
    buys_5m = txns.get("m5", {}).get("buys") or 0
    sells_5m = txns.get("m5", {}).get("sells") or 0

    return {
        "mcap": pair.get("marketCap") or 0,
        "liquidity_usd": liquidity.get("usd") or 0,
        "vol_5m": volume.get("m5") or 0,
        "age_hours": round(age_hours, 1),
        "buys_24h": txns.get("h24", {}).get("buys") or 0,
        "sells_24h": txns.get("h24", {}).get("sells") or 0,
        "txs_5m": buys_5m + sells_5m,
        "price_change_h1": price_change.get("h1"),
        "price_change_h6": price_change.get("h6"),
        "price_change_h24": price_change.get("h24"),
    }


def _fmt_num(n: float) -> str:
    """Format a number nicely with K/M/B."""
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.2f}"


# ── DexScreener API calls ──────────────────────────────────────────────

def _search_tokens(query: str) -> list[dict]:
    """Search DexScreener and return Solana pairs."""
    url = f"{DEXSCREENER_BASE}/latest/dex/search?q={query}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        return [p for p in pairs if p.get("chainId") == "solana"]
    except requests.RequestException as e:
        logger.warning("Search failed for '%s': %s", query, e)
        return []


def _fetch_token_pairs(token_address: str) -> list[dict]:
    """Fetch full pair data for a token address. Returns Solana pairs only."""
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        return [p for p in pairs if p.get("chainId") == "solana"]
    except requests.RequestException:
        return []


# ── Filter pipeline (mirrors scanner._check_filters) ──────────────────

def _check_filters(pair: dict) -> tuple[bool, str]:
    """
    Run the same filter pipeline as the live scanner.
    Returns (passed, rejected_by_reason).
    """
    txns = pair.get("txns", {})
    volume = pair.get("volume", {})
    liquidity = pair.get("liquidity", {})
    pair_created = pair.get("pairCreatedAt", 0)

    age_hours = _hours_since(pair_created) if pair_created else 999
    market_cap = pair.get("marketCap") or 0
    liq_usd = liquidity.get("usd") or 0
    vol_5m = volume.get("m5") or 0
    buys_24h = txns.get("h24", {}).get("buys") or 0
    sells_24h = txns.get("h24", {}).get("sells") or 0
    buys_5m = txns.get("m5", {}).get("buys") or 0
    sells_5m = txns.get("m5", {}).get("sells") or 0
    txs_5m = buys_5m + sells_5m

    if liq_usd < LIQUIDITY_MIN:
        return False, "liq"
    if not (MCAP_MIN <= market_cap <= MCAP_MAX):
        return False, "mcap"
    if age_hours > MAX_AGE_HOURS:
        return False, "age"
    if buys_24h < BUYS_24H_MIN:
        return False, "buys"
    if sells_24h < SELLS_24H_MIN:
        return False, "sells"
    if txs_5m < TXS_5M_MIN:
        return False, "txs"
    if vol_5m < VOL_5M_MIN:
        return False, "vol"
    return True, ""


# ── Outcome simulation ─────────────────────────────────────────────────

def _simulate_outcome(pair: dict) -> bool:
    """
    Estimate whether this token hit 2x at any point.
    Uses DexScreener's built-in price change percentages; if any window
    shows >= +100%, we consider it a success.
    """
    price_change = pair.get("priceChange", {})
    for window in ("m5", "h1", "h6", "h24"):
        change = price_change.get(window)
        if change is not None and change >= 100:
            return True
    return False


# ── DB helpers ──────────────────────────────────────────────────────────

def _init_backtest_table():
    """Create the backtest_results table if it doesn't exist."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address   TEXT,
            symbol          TEXT,
            mcap            REAL,
            liquidity_usd   REAL,
            vol_5m          REAL,
            age_hours       REAL,
            buys_24h        INTEGER,
            sells_24h       INTEGER,
            txs_5m          INTEGER,
            price_change_h1 REAL,
            price_change_h6 REAL,
            price_change_h24 REAL,
            passed_filters  INTEGER,
            rejected_by     TEXT,
            hit_2x          INTEGER,
            simulated_at    TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bt_passed
        ON backtest_results(passed_filters)
    """)
    conn.commit()
    conn.close()


def _save_result(token_address: str, symbol: str, metrics: dict,
                 passed: bool, rejected_by: str, hit_2x: bool):
    """Write one token's backtest result to the DB."""
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO backtest_results
            (token_address, symbol, mcap, liquidity_usd, vol_5m, age_hours,
             buys_24h, sells_24h, txs_5m, price_change_h1, price_change_h6,
             price_change_h24, passed_filters, rejected_by, hit_2x, simulated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        token_address, symbol,
        metrics["mcap"], metrics["liquidity_usd"],
        metrics["vol_5m"], metrics["age_hours"],
        metrics["buys_24h"], metrics["sells_24h"],
        metrics["txs_5m"],
        metrics["price_change_h1"], metrics["price_change_h6"],
        metrics["price_change_h24"],
        1 if passed else 0,
        rejected_by,
        1 if hit_2x else 0,
        now,
    ))
    conn.commit()
    conn.close()


# ── Reporting ──────────────────────────────────────────────────────────

def _print_report(stats: dict, mcap_buckets: dict, vol_buckets: dict,
                  rejection_reasons: dict):
    """Print the backtest report to stdout."""
    total = stats["total"]
    passed = stats["passed"]
    failed = stats["failed"]
    passed_success = stats["passed_success"]
    failed_success = stats["failed_success"]

    passed_rate = (passed / total * 100) if total else 0

    # Filtered success rate (only tokens that passed filters)
    filtered_success_rate = (
        passed_success / passed * 100 if passed else 0
    )
    # Unfiltered success rate (all tokens)
    all_success = passed_success + failed_success
    unfiltered_success_rate = (
        all_success / total * 100 if total else 0
    )

    # Missed opportunities (tokens that failed filters but still hit 2x)
    missed = failed_success

    print()
    print("=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print()
    print(f"  Total tokens scanned:         {total}")
    print(f"  Would have been alerted:       {passed} ({passed_rate:.1f}%)")
    print(f"  Filtered out:                  {failed} ({100 - passed_rate:.1f}%)")
    print()
    print(f"  ── Success Analysis ──")
    print(f"  Alerted & hit 2x:              {passed_success}")
    print(f"  Alerted & missed:              {passed - passed_success}")
    print(f"  Filtered success rate:         {filtered_success_rate:.1f}%")
    print()
    print(f"  Missed opportunities           {missed}")
    print(f"    (failed filters but hit 2x)")
    print(f"  Unfiltered success rate:       {unfiltered_success_rate:.1f}%")
    if filtered_success_rate > unfiltered_success_rate and unfiltered_success_rate > 0:
        improvement = (
            (filtered_success_rate - unfiltered_success_rate)
            / unfiltered_success_rate * 100
        )
        print(f"  Filter improvement:           +{improvement:.0f}% vs unfiltered")
    print()

    # ── Rejection breakdown ──
    print(f"  ── Rejection Breakdown ──")
    for reason, count in sorted(
        rejection_reasons.items(), key=lambda x: -x[1]
    ):
        pct = count / failed * 100 if failed else 0
        labels = {
            "liq":   f"Liquidity < ${LIQUIDITY_MIN:,}",
            "mcap":  f"MCap outside ${MCAP_MIN:,}-${MCAP_MAX:,}",
            "age":   f"Age > {MAX_AGE_HOURS}h",
            "buys":  f"Buys(24h) < {BUYS_24H_MIN}",
            "sells": f"Sells(24h) < {SELLS_24H_MIN}",
            "txs":   f"Txs(5m) < {TXS_5M_MIN}",
            "vol":   f"Vol(5m) < ${VOL_5M_MIN:,}",
        }
        label = labels.get(reason, reason)
        print(f"    {label:<35} {count:>4} ({pct:>5.1f}%)")
    print()

    # ── Optimal MCap range ──
    print(f"  ── Optimal MCap Range (alerts only) ──")
    sorted_buckets = sorted(mcap_buckets.items(), key=lambda x: x[0])
    # Show buckets with >= 2 datapoints
    valid = [(b, d) for b, d in sorted_buckets if d["total"] >= 2]
    if valid:
        best = max(valid, key=lambda x: x[1]["success"] / x[1]["total"])
        print(f"    Best bucket:   ${best[0]:,.0f}-${best[0]+100_000:,.0f}")
        print(f"    Success rate:  {best[1]['success']}/{best[1]['total']} "
              f"({best[1]['success']/best[1]['total']*100:.1f}%)")
        print()
        print(f"    {'Bucket':<20} {'Total':>6} {'Hits':>6} {'Rate':>8}")
        print(f"    {'-'*40}")
        for bucket, data in sorted_buckets:
            rate = data["success"] / data["total"] * 100 if data["total"] else 0
            bar = "█" * max(1, int(rate / 10))
            print(f"    ${bucket:>8,}-${bucket+100_000:<7,} "
                  f"{data['total']:>4} {data['success']:>4} "
                  f"{rate:>6.1f}%  {bar}")
    else:
        print("    (Insufficient data — need >= 2 alerts per bucket)")
    print()

    # ── Volume thresholds ──
    print(f"  ── Optimal Volume Threshold (alerts only) ──")
    sorted_vol = sorted(vol_buckets.items(), key=lambda x: x[0])
    valid_vol = [(b, d) for b, d in sorted_vol if d["total"] >= 2]
    if valid_vol:
        best_vol = max(valid_vol, key=lambda x: x[1]["success"] / x[1]["total"])
        print(f"    Best bucket:   ${best_vol[0]:,.0f}-${best_vol[0]+10_000:,.0f}")
        print(f"    Success rate:  {best_vol[1]['success']}/{best_vol[1]['total']} "
              f"({best_vol[1]['success']/best_vol[1]['total']*100:.1f}%)")
        print()
        print(f"    {'Bucket':<20} {'Total':>6} {'Hits':>6} {'Rate':>8}")
        print(f"    {'-'*40}")
        for bucket, data in sorted_vol:
            rate = data["success"] / data["total"] * 100 if data["total"] else 0
            bar = "█" * max(1, int(rate / 10))
            print(f"    ${bucket:>8,}-${bucket+10_000:<7,} "
                  f"{data['total']:>4} {data['success']:>4} "
                  f"{rate:>6.1f}%  {bar}")
    else:
        print("    (Insufficient data — need >= 2 alerts per bucket)")
    print()


# ── Main entry point ───────────────────────────────────────────────────

def run_backtest():
    """
    One-off backtest: search DexScreener for Solana tokens, run the
    filter pipeline on each, simulate outcomes, save results to DB,
    and print a comprehensive report.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("  BACKTEST — Filter Pipeline Evaluation")
    print("  Fetching historical tokens from DexScreener...")
    print("=" * 60)
    print()

    _init_backtest_table()

    # ── 1. Collect a broad sample of Solana tokens ────────────────
    # Use letter + number prefixes for broad coverage
    search_terms = [
        "a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
        "k", "l", "m", "n", "o", "p", "q", "r", "s", "t",
        "u", "v", "w", "x", "y", "z",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    ]

    all_pairs: dict[str, dict] = {}

    print(f"  Searching DexScreener with {len(search_terms)} queries...")
    for term in search_terms:
        pairs = _search_tokens(term)
        for p in pairs:
            addr = (
                p.get("tokenAddress")
                or p.get("baseToken", {}).get("address")
            )
            if addr and addr not in all_pairs:
                all_pairs[addr] = p
        time.sleep(0.1)

    print(f"  Found {len(all_pairs)} unique Solana tokens.")
    print()

    # ── 2. Run filter pipeline on each token ──────────────────────
    stats = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "passed_success": 0,
        "failed_success": 0,
    }
    rejection_reasons: dict[str, int] = {}
    mcap_buckets: dict[int, dict] = {}
    vol_buckets: dict[int, dict] = {}

    print(f"  Running filter pipeline and outcome simulation...")
    idx = 0

    for addr in all_pairs:
        idx += 1
        stats["total"] += 1
        base_token = all_pairs[addr].get("baseToken", {})
        symbol = base_token.get("symbol", "?")

        # Fetch fresh pair data with full metrics
        fresh_pairs = _fetch_token_pairs(addr)
        if not fresh_pairs:
            continue
        pair = fresh_pairs[0]

        metrics = _extract_metrics(pair)
        passed, reason = _check_filters(pair)
        hit_2x = _simulate_outcome(pair)

        _save_result(addr, symbol, metrics, passed, reason, hit_2x)

        if passed:
            stats["passed"] += 1
            if hit_2x:
                stats["passed_success"] += 1

            # Track mcap bucket (100k increments)
            mcap = metrics["mcap"]
            mcap_b = int(mcap / 100_000) * 100_000
            if mcap_b not in mcap_buckets:
                mcap_buckets[mcap_b] = {"total": 0, "success": 0}
            mcap_buckets[mcap_b]["total"] += 1
            if hit_2x:
                mcap_buckets[mcap_b]["success"] += 1

            # Track vol bucket (10k increments)
            vol = metrics["vol_5m"]
            vol_b = int(vol / 10_000) * 10_000
            if vol_b not in vol_buckets:
                vol_buckets[vol_b] = {"total": 0, "success": 0}
            vol_buckets[vol_b]["total"] += 1
            if hit_2x:
                vol_buckets[vol_b]["success"] += 1
        else:
            stats["failed"] += 1
            if hit_2x:
                stats["failed_success"] += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        time.sleep(0.05)

    # ── 3. Print report ───────────────────────────────────────────
    _print_report(stats, mcap_buckets, vol_buckets, rejection_reasons)
    print(f"  Results saved to backtest_results table in bot.db")
    print()
    print("  Done.")
    print("=" * 60)