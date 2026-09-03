"""
milestones.py — Multiplier milestone cards for bot calls.

While a token the bot called keeps climbing, every new multiple crossed
from the ladder below generates a celebration card until the coin stops
making new highs. Wins only — losses are handled by the tracker and
never get a milestone card.

Ladder: 1.5x → 2x → 3x → 5x → 10x → 20x → 40x → 100x

State lives on alerts.last_milestone (highest milestone announced so far),
so each milestone fires exactly once even if the price oscillates.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from config import DEXSCREENER_BASE
from bot.models import execute, close_cursor, commit, _dict_rows

logger = logging.getLogger(__name__)

MILESTONES = [1.5, 2, 3, 5, 10, 20, 40, 100]
LOOKBACK_DAYS = 3          # how long a call stays milestone-eligible
BATCH_SIZE = 30            # DexScreener allows 30 addresses per request


def _recent_alerts() -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    c = execute(
        "SELECT id, token_address, symbol, alert_mcap, last_milestone, alert_time "
        "FROM alerts WHERE alert_time >= %s AND alert_mcap > 0",
        (cutoff,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def _fetch_mcaps(addresses: list[str]) -> dict[str, float]:
    """Batch current-mcap lookup on DexScreener (solana pairs, best pair per token)."""
    out: dict[str, float] = {}
    for i in range(0, len(addresses), BATCH_SIZE):
        batch = addresses[i:i + BATCH_SIZE]
        try:
            resp = requests.get(
                f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(batch)}",
                timeout=15)
            resp.raise_for_status()
            for p in resp.json().get("pairs") or []:
                if p.get("chainId") != "solana":
                    continue
                addr = (p.get("baseToken") or {}).get("address")
                mcap = p.get("marketCap") or 0
                if addr and mcap > out.get(addr, 0):
                    out[addr] = mcap
        except requests.RequestException as e:
            logger.debug("DexScreener batch mcap failed: %s", e)
    return out


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def check_milestones() -> list[dict]:
    """
    Check all recent bot calls for newly crossed multiplier milestones.

    Returns one event per newly crossed milestone:
      {"symbol", "token_address", "multiplier", "alert_mcap",
       "current_mcap", "duration"}
    """
    alerts = _recent_alerts()
    if not alerts:
        return []

    mcaps = _fetch_mcaps([a["token_address"] for a in alerts])
    now = datetime.now(timezone.utc)
    events = []

    for a in alerts:
        current = mcaps.get(a["token_address"])
        alert_mcap = a.get("alert_mcap")
        if not current or not alert_mcap:
            continue

        last = float(a.get("last_milestone") or 0)
        mult = current / alert_mcap
        crossed = [m for m in MILESTONES if m > last and m <= mult]
        if not crossed:
            continue

        duration = None
        alert_time = a.get("alert_time")
        if alert_time:
            try:
                t = alert_time if isinstance(alert_time, datetime) \
                    else datetime.fromisoformat(str(alert_time))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                duration = _fmt_duration((now - t).total_seconds())
            except ValueError:
                pass

        for m in crossed:
            events.append({
                "symbol": a["symbol"],
                "token_address": a["token_address"],
                "multiplier": m,
                "alert_mcap": alert_mcap,
                "current_mcap": current,
                "duration": duration,
            })
        _set_last_milestone(a["id"], max(crossed))
        logger.info("🚀 %s hit %s (announcing %d milestone card(s))",
                    a["symbol"], f"{max(crossed):g}x", len(crossed))

    return events


def _set_last_milestone(alert_id: int, milestone: float):
    c = execute("UPDATE alerts SET last_milestone = %s WHERE id = %s",
                (float(milestone), alert_id))
    close_cursor(c)
    commit()
