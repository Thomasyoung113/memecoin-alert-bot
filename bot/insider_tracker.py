"""
Insider tracker — monitors whether dev/insider wallets are dumping
after alerts by comparing top holder balances over time.
"""
import json
import logging
import time

import requests

from config import RUGCHECK_BASE
from bot.models import execute, close_cursor, commit, _dict_rows, get_pending_alerts

logger = logging.getLogger(__name__)

# Module-level rate limiting for RugCheck calls (30s between calls)
_last_rugcheck_call = 0.0


def _rate_limited_rugcheck(url: str, token_address: str) -> dict | None:
    """Make a RugCheck call with 30-second minimum interval between calls."""
    global _last_rugcheck_call
    now = time.time()
    elapsed = now - _last_rugcheck_call
    if elapsed < 30.0:
        sleep_time = 30.0 - elapsed
        logger.debug("RugCheck rate limit: sleeping %.1fs for %s",
                     sleep_time, token_address[:8])
        time.sleep(sleep_time)

    try:
        resp = requests.get(url, timeout=15)
        _last_rugcheck_call = time.time()
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.debug("RugCheck request failed for %s: %s",
                     token_address[:8], e)
        # Still advance the timer to avoid hammering on errors
        _last_rugcheck_call = time.time()
        return None


def _fetch_top_holders(token_address: str) -> list[dict] | None:
    """
    Fetch top holders from the full RugCheck token report.

    Returns a list of dicts with keys 'address', 'balance', 'pct'
    for the top 5 holders, or None on failure.
    """
    url = f"{RUGCHECK_BASE}/tokens/{token_address}/report"
    data = _rate_limited_rugcheck(url, token_address)
    if not data:
        return None

    raw_holders = data.get("topHolders")
    if not raw_holders or not isinstance(raw_holders, list):
        logger.debug("No topHolders in RugCheck report for %s",
                     token_address[:8])
        return None

    holders = []
    for h in raw_holders[:5]:
        holders.append({
            "address": h.get("address", ""),
            "balance": float(h.get("balance", 0)),
            "pct": float(h.get("pct", 0)),
        })
    return holders


def _get_holder_baseline(alert_id: int) -> list[dict] | None:
    """Read the holder baseline JSON from the DB for a given alert."""
    c = execute("SELECT holder_baseline FROM alerts WHERE id = %s", (alert_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    if rows and rows[0]["holder_baseline"]:
        try:
            return json.loads(rows[0]["holder_baseline"])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _set_holder_baseline(alert_id: int, holders: list[dict]):
    """Persist holder baseline JSON to the DB."""
    c = execute(
        "UPDATE alerts SET holder_baseline = %s WHERE id = %s",
        (json.dumps(holders), alert_id),
    )
    close_cursor(c)
    commit()


def check_insider_selling(token_address: str, snapshot: dict) -> dict:
    """
    Check whether insider/dev wallets are dumping by comparing current
    top-5 holder balances against the stored baseline for the token.

    Args:
        token_address: The token mint address to check.
        snapshot: The scanner snapshot dict from alert time (used for
                  additional context; holder baseline is stored separately).

    Returns:
        A dict with keys:
          is_dumping (bool): True if collective top-5 balance dropped >10%.
          details (str): Human-readable explanation.
    """
    # ── Fetch current top holders ─────────────────────────────────────
    current = _fetch_top_holders(token_address)
    if not current:
        return {
            "is_dumping": False,
            "details": "Could not fetch current top holders from RugCheck",
        }

    # ── Look up alert and its baseline ────────────────────────────────
    c = execute(
        "SELECT id, holder_baseline FROM alerts WHERE token_address = %s",
        (token_address,))
    alerts = _dict_rows(c)
    close_cursor(c)

    if not alerts:
        return {
            "is_dumping": False,
            "details": "Token not found in alerts table",
        }

    alert = alerts[0]
    baseline = _get_holder_baseline(alert["id"])

    if baseline is None:
        # First time checking this token — record baseline, no flag
        _set_holder_baseline(alert["id"], current)
        total_pct = sum(h["pct"] for h in current)
        logger.info("Recorded holder baseline for %s: top-5 own %.1f%%",
                    token_address[:8], total_pct)
        return {
            "is_dumping": False,
            "details": f"Baseline recorded (top-5: {total_pct:.1f}%)",
        }

    # ── Compare baseline vs current — matched by wallet address ──────
    baseline_by_addr = {h["address"]: h for h in baseline}
    current_by_addr = {h["address"]: h for h in current}

    common = set(baseline_by_addr.keys()) & set(current_by_addr.keys())

    if not common:
        # Completely different top holders — could mean heavy churn
        # Update baseline and flag cautiously
        _set_holder_baseline(alert["id"], current)
        return {
            "is_dumping": False,
            "details": "Top holders completely changed — baseline reset",
        }

    total_baseline_pct = sum(baseline_by_addr[addr]["pct"] for addr in common)
    total_current_pct = sum(current_by_addr[addr]["pct"] for addr in common)

    if total_baseline_pct <= 0:
        return {
            "is_dumping": False,
            "details": "Baseline had zero allocation for tracked holders",
        }

    change_pct = ((total_baseline_pct - total_current_pct)
                  / total_baseline_pct) * 100.0

    # Update baseline so next comparison uses the latest data
    _set_holder_baseline(alert["id"], current)

    if change_pct > 10.0:
        logger.warning("Insider selling detected for %s: %.1f%% drop",
                       token_address[:8], change_pct)
        return {
            "is_dumping": True,
            "details": (
                f"Insider selling detected: top-5 holder balance dropped "
                f"{change_pct:.1f}% (was {total_baseline_pct:.1f}%, "
                f"now {total_current_pct:.1f}%)"
            ),
        }

    return {
        "is_dumping": False,
        "details": (
            f"Top holders stable ({change_pct:+.1f}% change, "
            f"current: {total_current_pct:.1f}%)"
        ),
    }


def check_insider_selling_for_alerts() -> list[dict]:
    """
    Run the insider selling check across all pending (unresolved) alerts.

    Returns a list of result dicts for alerts where dumping was detected:
      [{symbol, token_address, alert_mcap, details}, ...]
    """
    pending = get_pending_alerts()
    if not pending:
        return []

    results = []
    for alert in pending:
        # Parse the stored scan snapshot for context (may be empty)
        snapshot = {}
        raw = alert.get("scan_snapshot")
        if raw:
            try:
                snapshot = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

        result = check_insider_selling(alert["token_address"], snapshot)
        if result["is_dumping"]:
            # Fetch current MCap for the insider alert display
            from bot.tracker import _fetch_current_mcap
            current_mcap = _fetch_current_mcap(alert["token_address"])
            results.append({
                "symbol": alert["symbol"],
                "token_address": alert["token_address"],
                "alert_mcap": alert["alert_mcap"],
                "current_mcap": current_mcap or alert["alert_mcap"],
                "details": result["details"],
            })
            logger.warning("Insider selling confirmed for $%s: %s",
                           alert["symbol"], result["details"])

    return results