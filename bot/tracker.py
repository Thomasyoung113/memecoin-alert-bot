"""
Outcome tracker — monitors alerted tokens to see if they hit 2x or -50%.
Every 60s check evaluates each pending token for both outcomes.
"""
import logging

import requests

from config import DEXSCREENER_BASE, LOSS_TARGET
from bot.models import get_pending_alerts, mark_resolved

logger = logging.getLogger(__name__)


def _fetch_current_mcap(token_address: str) -> float | None:
    """Fetch the current market cap from DexScreener."""
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        for p in pairs:
            if p.get("chainId") == "solana":
                return p.get("marketCap") or 0
    except requests.RequestException as e:
        logger.debug("Failed to check %s: %s", token_address[:8], e)
    return None


def check_outcomes() -> list[dict]:
    """
    Check all pending alerts to see if they hit 2x (win) or -50% (loss).

    Returns a list of resolution dicts:
      [{"symbol", "alert_mcap", "target_mcap", "hit_2x", "hit_loss",
        "peak_mcap", "current_mcap", "token_address"}, ...]
    """
    pending = get_pending_alerts()
    if not pending:
        return []

    resolutions = []

    for alert in pending:
        addr = alert["token_address"]
        current_mcap = _fetch_current_mcap(addr)

        if current_mcap is None:
            continue

        target_mcap = alert["target_2x_mcap"]
        alert_mcap = alert["alert_mcap"]
        loss_mcap = alert_mcap * LOSS_TARGET

        # Update peak if needed
        peak = alert.get("peak_mcap") or alert_mcap
        new_peak = max(peak, current_mcap)

        hit_2x = current_mcap >= target_mcap or new_peak >= target_mcap
        hit_loss = current_mcap <= loss_mcap

        if hit_2x:
            mark_resolved(addr, True, new_peak)
            resolutions.append({
                "symbol": alert["symbol"],
                "alert_mcap": alert_mcap,
                "target_mcap": target_mcap,
                "hit_2x": True,
                "hit_loss": False,
                "peak_mcap": new_peak,
                "current_mcap": current_mcap,
                "token_address": addr,
            })
            logger.info("✅ %s hit 2x! (MCap: %.0f -> %.0f)",
                        alert["symbol"], alert_mcap, new_peak)
        elif hit_loss:
            mark_resolved(addr, False, new_peak, hit_loss=True)
            resolutions.append({
                "symbol": alert["symbol"],
                "alert_mcap": alert_mcap,
                "target_mcap": target_mcap,
                "hit_2x": False,
                "hit_loss": True,
                "peak_mcap": new_peak,
                "current_mcap": current_mcap,
                "token_address": addr,
            })
            logger.info("❌ %s hit -50%% loss! (MCap: %.0f -> %.0f)",
                        alert["symbol"], alert_mcap, current_mcap)
        else:
            # Update peak even if not resolved
            if new_peak > peak:
                _update_peak(alert["id"], new_peak)

    return resolutions


def _update_peak(alert_id: int, peak_mcap: float):
    """Update the peak_mcap for an alert."""
    from bot.models import get_conn
    conn = get_conn()
    conn.execute(
        "UPDATE alerts SET peak_mcap = ? WHERE id = ?",
        (peak_mcap, alert_id)
    )
    conn.commit()
    conn.close()


def force_resolve_stale():
    """
    Mark any pending alerts that have exceeded the max check window as
    resolved (failed). This prevents unbounded growth.

    Returns number of stale alerts resolved.
    """
    from bot.models import get_conn

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, token_address, symbol, alert_mcap, peak_mcap "
        "FROM alerts WHERE resolved = 0"
    ).fetchall()
    resolved_count = 0
    for r in rows:
        peak = r["peak_mcap"] or r["alert_mcap"]
        mark_resolved(r["token_address"], False, peak)
        resolved_count += 1

    if resolved_count:
        logger.info("Force-resolved %d stale alerts", resolved_count)

    conn.close()
    return resolved_count