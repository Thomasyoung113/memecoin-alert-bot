"""
Smart money profiler — scores wallets based on early-buy success rate
and determines which wallets are "smart money".
"""
import logging

from config import SMART_WALLET_MIN_HITS, SMART_WALLET_MIN_SUCCESS_RATE
from bot.models import execute, close_cursor, commit, _dict_rows

logger = logging.getLogger(__name__)


def score_wallet(wallet_address: str) -> dict:
    """
    Score a single wallet based on its early-buy track record.

    Returns dict with stats:
      {address, total_early_buys, successful_buys, total_calls, hit_rate, is_smart}
    """
    c = execute(
        "SELECT total_early_buys, successful_buys, total_calls "
        "FROM wallets WHERE address = %s", (wallet_address,))
    rows = _dict_rows(c)
    close_cursor(c)

    if not rows:
        return {
            "address": wallet_address,
            "total_early_buys": 0,
            "successful_buys": 0,
            "total_calls": 0,
            "hit_rate": 0.0,
            "is_smart": False,
        }

    total = rows[0]["total_early_buys"]
    successful = rows[0]["successful_buys"]
    calls = rows[0]["total_calls"]

    hit_rate = (successful / total * 100) if total > 0 else 0.0
    is_smart = (total >= SMART_WALLET_MIN_HITS
                and hit_rate >= SMART_WALLET_MIN_SUCCESS_RATE * 100)

    return {
        "address": wallet_address,
        "total_early_buys": total,
        "successful_buys": successful,
        "total_calls": calls,
        "hit_rate": round(hit_rate, 1),
        "is_smart": is_smart,
    }


def update_wallet_success(wallet_address: str, token_hit_2x: bool):
    """
    Update a wallet's success stats when a token they bought early resolves.
    """
    c = execute(
        "UPDATE wallets SET total_calls = total_calls + 1, "
        "successful_buys = successful_buys + %s "
        "WHERE address = %s",
        (1 if token_hit_2x else 0, wallet_address))
    close_cursor(c)
    commit()


def get_smart_wallets_for_token(token_address: str) -> list[dict]:
    """
    Find known smart wallets that bought early on a specific token.

    Returns list of wallet dicts sorted by hit rate descending.
    """
    c = execute("""
        SELECT w.address, w.total_early_buys, w.successful_buys,
               w.total_calls, wb.buy_position
        FROM wallet_buys wb
        JOIN wallets w ON w.address = wb.wallet_address
        WHERE wb.token_address = %s AND w.is_smart = 1
        ORDER BY wb.buy_position ASC
    """, (token_address,))
    rows = _dict_rows(c)
    close_cursor(c)

    results = []
    for r in rows:
        hit_rate = (r["successful_buys"] / r["total_early_buys"] * 100
                    if r["total_early_buys"] > 0 else 0)
        results.append({
            "address": r["address"],
            "total_early_buys": r["total_early_buys"],
            "successful_buys": r["successful_buys"],
            "hit_rate": round(hit_rate, 1),
            "buy_position": r["buy_position"],
        })

    return results


def refresh_smart_flags():
    """
    Re-evaluate all wallets and update their is_smart flag.
    Run periodically as the DB grows.
    """
    c = execute(
        "SELECT address, total_early_buys, successful_buys FROM wallets")
    wallets = _dict_rows(c)
    close_cursor(c)

    updated = 0
    for w in wallets:
        total = w["total_early_buys"]
        successful = w["successful_buys"]
        hit_rate = (successful / total) if total > 0 else 0
        is_smart = (total >= SMART_WALLET_MIN_HITS
                    and hit_rate >= SMART_WALLET_MIN_SUCCESS_RATE)

        c = execute(
            "UPDATE wallets SET is_smart = %s WHERE address = %s",
            (1 if is_smart else 0, w["address"]))
        close_cursor(c)
        if is_smart:
            updated += 1

    commit()
    logger.info("Smart flags refreshed: %d smart wallets", updated)
    return updated


def mark_token_success_for_wallets(token_address: str, hit_2x: bool = True):
    """
    When an alerted token resolves, update all wallets that bought it early.
    """
    c = execute(
        "SELECT wallet_address FROM wallet_buys WHERE token_address = %s",
        (token_address,))
    rows = _dict_rows(c)
    close_cursor(c)

    for row in rows:
        update_wallet_success(row["wallet_address"], hit_2x)

    refresh_smart_flags()
    logger.info("Updated %d wallets for token %s (hit_2x=%s)",
                len(rows), token_address[:8], hit_2x)
