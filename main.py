#!/usr/bin/env python3
"""
Alert Bot — main entry point.

Runs the scanner loop, outcome checker, learning engine,
Telegram /start listener, and wide scanner (Phase 2).
"""
import time
import logging
import sys

from config import (
    POLL_INTERVAL, OUTCOME_CHECK_INTERVAL, LEARN_INTERVAL,
    TWO_X_TARGET, HELIUS_API_KEY,
    WHALE_WATCH_INTERVAL, WHALE_WATCH_LIST,
)
from bot.models import init_db, save_alert, get_stats
from bot.scanner import scan
from bot.checker import is_safe
from bot.telegram import (
    send_alert, send_resolved, send_learning_update, send_update,
)
from bot.tracker import check_outcomes, force_resolve_stale
from bot.insider_tracker import check_insider_selling_for_alerts
from bot.learner import run_learning_cycle, get_overall_success_rate
from bot.listener import start_listener
from bot.helius import init_helius
from bot.wide_scanner import start_wide_scanner
from bot.whale_watcher import start_whale_watcher
from bot.smart_money import get_smart_wallets_for_token, mark_token_success_for_wallets
from dashboard.server import start_dashboard

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def _fmt_interval(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds//3600}h"
    if seconds >= 60:
        return f"{seconds//60}m"
    return f"{seconds}s"


def main():
    print("=" * 50)
    print("  🚀 ALERT BOT — Memecoin Gem Spotter")
    print("  Phase 2: Smart Money Detection Active")
    print("=" * 50)
    print()

    # ── Init ──────────────────────────────────────────────────────
    logger.info("Initializing database...")
    init_db()

    # Init Helius RPC (optional, uses public RPC if no key)
    init_helius(HELIUS_API_KEY)

    total_alerts, total_success, success_rate = get_stats()
    logger.info("Track record: %d/%d resolved — %.1f%% success rate",
                total_success, total_alerts, success_rate)

    # ── Start background threads ──────────────────────────────────
    start_listener()                       # Telegram /start handler
    start_wide_scanner()                   # Phase 2: $10M wide net
    start_whale_watcher()                  # Whale wallet monitor
    start_dashboard()                      # Web dashboard on port 8080

    # ── State ─────────────────────────────────────────────────────
    last_outcome_check = 0
    last_learn = 0
    iteration = 0

    # Force-resolve any stale entries from previous runs
    stale = force_resolve_stale()
    if stale > 0:
        logger.info("Cleaned up %d stale entries", stale)

    print()
    print(f"  Scan interval:      {_fmt_interval(POLL_INTERVAL)}")
    print(f"  Outcome check:      {_fmt_interval(OUTCOME_CHECK_INTERVAL)}")
    print(f"  Learning cycle:     {_fmt_interval(LEARN_INTERVAL)}")
    print(f"  Wide scan:          10m (tokens >= $10M MCap)")
    print(f"  Whale watch:        {_fmt_interval(WHALE_WATCH_INTERVAL)} ({len(WHALE_WATCH_LIST)} wallets)")
    print(f"  Telegram listener:  active (/start welcome)")
    print(f"  Tracking:           {total_alerts} alerts, {success_rate}% success")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    # ── Main loop ─────────────────────────────────────────────────
    while True:
        try:
            now = time.time()

            # ── 1. SCAN for new tokens ────────────────────────────
            logger.info("Scanning for new tokens...")
            candidates = scan()

            for token_address, symbol, mcap, price, snapshot in candidates:
                logger.info("Processing candidate: $%s (MCap: $%.0f)",
                            symbol, mcap)

                # Safety check via RugCheck
                safe, safety_detail = is_safe(token_address)
                if not safe:
                    logger.info("  └─ Unsafe: %s",
                                safety_detail.get("reason", "unknown"))
                    continue

                # Phase 2: Check if any known smart wallets bought early on this token
                smart_wallets = get_smart_wallets_for_token(token_address)

                # Save to DB
                target_mcap = mcap * TWO_X_TARGET
                save_alert(token_address, symbol, mcap, price,
                           target_mcap, snapshot)

                # Send Telegram alert
                send_alert(
                    token_address, symbol, mcap, price,
                    snapshot, safety_detail, smart_wallets,
                )

                logger.info("  └─ ✅ Alert sent! Target: $%.0f"
                            + (" (Smart Money: %d wallets)" % len(smart_wallets)
                               if smart_wallets else ""),
                            target_mcap)

            if candidates:
                total_alerts, total_success, success_rate = get_stats()
                logger.info("Track record: %d/%d — %.1f%%",
                            total_success, total_alerts, success_rate)

            # ── 2. CHECK outcomes ─────────────────────────────────
            if now - last_outcome_check >= OUTCOME_CHECK_INTERVAL:
                logger.info("Checking outcomes...")
                resolutions = check_outcomes()
                for r in resolutions:
                    send_resolved(
                        r["symbol"], r["alert_mcap"],
                        r.get("target_mcap", r["alert_mcap"] * 2),
                        r["hit_2x"], r["peak_mcap"],
                    )
                    # Phase 2: Update smart wallet stats based on resolution
                    # (This will be wired once we have token_address in resolutions)
                if resolutions:
                    total_alerts, total_success, success_rate = get_stats()
                    logger.info("Track record after resolution: %d/%d — %.1f%%",
                                total_success, total_alerts, success_rate)

                # ── 2b. Check insider selling on unresolved alerts ──
                insider_alerts = check_insider_selling_for_alerts()
                for ia in insider_alerts:
                    msg = (
                        f"⚠️ <b>Insider Selling — ${ia['symbol']}</b>\n"
                        f"{ia['details']}\n"
                        f"MCap at alert: ${ia['alert_mcap']:,.0f}"
                    )
                    send_update(msg)
                if insider_alerts:
                    logger.warning("Insider selling detected on %d token(s)",
                                   len(insider_alerts))

                last_outcome_check = now

            # ── 3. LEARN ──────────────────────────────────────────
            if now - last_learn >= LEARN_INTERVAL:
                logger.info("Running learning cycle...")
                iteration += 1
                changes = run_learning_cycle(iteration)
                if changes:
                    new_rate = get_overall_success_rate()
                    send_learning_update(iteration, changes, new_rate)
                    logger.info("Learning: %d adjustments made", len(changes))
                else:
                    logger.info("Learning: no adjustments needed")
                last_learn = now

            # ── Sleep ─────────────────────────────────────────────
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print()
            logger.info("Shutting down...")
            total_alerts, total_success, success_rate = get_stats()
            print()
            print("=" * 50)
            print("  📊 FINAL STATS")
            print(f"  Total alerts:     {total_alerts}")
            print(f"  Successful (2x):  {total_success}")
            print(f"  Success rate:     {success_rate}%")
            print("=" * 50)
            sys.exit(0)

        except Exception as e:
            logger.exception("Unhandled error in main loop: %s", e)
            time.sleep(POLL_INTERVAL)
            continue


if __name__ == "__main__":
    main()