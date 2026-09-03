"""
wash_detector.py — Pump.fun creator-revenue wash-trading gate (Phase 4).

Pump.fun pays the creator 0.05% of every trade, so creators farm revenue
by churn-trading their own coin through funded side wallets. That churn
inflates exactly the numbers this bot calls on (buys_24h, sells_24h,
txs_5m, vol_5m) while the coin has no real exit liquidity.

wash_score (0-100, higher = more fake). Decision spec (owner):
  - wash_score >= WASH_SCORE_MAX (35)   → NO CALL at all
  - auto-buy requires < WASH_SCORE_AUTOBUY_MAX (20) — stricter than calls
  - thresholds FIXED for the first month (no learner tuning); alerts
    store score + detail so month-2 tuning is data-driven
  - latency budget 5s/candidate: the pure-math signals are free, and the
    creator/insider signals ride the RugCheck report is_safe already
    fetches — no extra RPC on the call path

Signals:
  +20  buy/sell symmetry (ratio in WASH_SYMMETRY_BAND) AND flat price
  +20  volume/price divergence (sustained volume, dead price)
  +15  creator round-tripping own coin (creator among first buyers)
  +25  serial flippers dominate first buyers (DB behavioral join)
  +20  buyer-cluster traveling together across tokens (DB join)
  RugCheck creator authenticity (graphInsidersDetected / insiderNetworks
  / rugged creator history) adds via score_rugcheck_creator() — same
  report the safety gate already downloaded.
"""
import logging

from config import (
    WASH_SCORE_MAX, WASH_SCORE_AUTOBUY_MAX,
    WASH_SYMMETRY_BAND, WASH_FLAT_PRICE_H1,
    WASH_SERIAL_FLIPPER_TOKENS, WASH_CLUSTER_MIN_JOURNEYS,
)

logger = logging.getLogger(__name__)


# ── Pure-math signals (zero API cost) ─────────────────────────────────

def score_pair_math(pair: dict) -> dict:
    txns = pair.get("txns", {})
    buys_24h = txns.get("h24", {}).get("buys") or 0
    sells_24h = txns.get("h24", {}).get("sells") or 0
    price_h1 = abs(pair.get("priceChange", {}).get("h1") or 0)
    price_m5 = abs(pair.get("priceChange", {}).get("m5") or 0)

    signals, score = {}, 0

    # Signal 2: churn loop keeps buys≈sells while price sits still
    ratio = (buys_24h / sells_24h) if sells_24h else 99.0
    low, high = WASH_SYMMETRY_BAND
    symmetric = sells_24h >= 10 and low <= ratio <= high
    flat = price_h1 < WASH_FLAT_PRICE_H1 and price_m5 < (WASH_FLAT_PRICE_H1 / 2)
    if symmetric and flat:
        score += 20
    signals["buy_sell_ratio"] = round(ratio, 2)
    signals["symmetry_flag"] = bool(symmetric and flat)

    # Signal 3: sustained volume + dead price across both windows
    vol_5m = pair.get("volume", {}).get("m5") or 0
    vol_1h = pair.get("volume", {}).get("h1") or 0
    divergence = (vol_5m >= 10_000 and vol_1h >= 50_000
                  and price_h1 < 1.0 and price_m5 < 1.0)
    signals["divergence_flag"] = divergence
    if divergence:
        score += 20
    signals["vol_5m"] = vol_5m
    signals["vol_1h"] = vol_1h

    return {"wash_score": score, "signals": signals}


# ── RugCheck creator authenticity (rides the existing safety call) ────

def score_rugcheck_creator(full_report: dict | None) -> dict:
    """Creator-reputation signals from the FULL RugCheck report.

    Uses creator balances / insider-graph / rugged history — the fields
    the summary endpoint drops but is_safe's full-report upgrade provides.
    """
    signals, score = {}, 0
    if not full_report:
        signals["creator_known"] = False
        return {"wash_score": 0, "signals": signals}

    creator = full_report.get("creator")
    signals["creator"] = creator
    signals["creator_known"] = bool(creator)

    # Creator still bagholding a large share — aligned to dump, not farm,
    # but still a distribution risk worth counting into wash score.
    try:
        balance = float(full_report.get("creatorBalance") or 0)
        supply = float(full_report.get("token", {}).get("supply") or 0)
        pct = (balance / supply * 100) if supply else 0
        signals["creator_supply_pct"] = round(pct, 2)
        if pct > 5:
            score += 10
    except (TypeError, ValueError):
        pass

    # RugCheck's own insider-network graph on this token
    if full_report.get("graphInsidersDetected"):
        score += 15
    insiders = full_report.get("insiderNetworks") or []
    if insiders:
        try:
            if len(insiders) >= 2:
                score += 10
        except (TypeError, ValueError):
            pass
    signals["insider_networks"] = len(insiders) if insiders else 0

    # Deploy platform: pump.fun launches get churn-checked harder; the
    # signal set above targets exactly that profile. No extra score here —
    # the platform is recorded for month-2 analysis.
    signals["launchpad"] = full_report.get("launchpad")

    return {"wash_score": min(100, score), "signals": signals}


# ── Behavioral DB joins (cheap, uses accumulated wallet_buys data) ────

def score_first_buyers(buyers: list[dict], creator: str | None = None) -> dict:
    """Signals over the first-buyer wallets already profiled in the DB.

    Runs inside the 5s budget: two indexed queries, no RPC tracing.
    """
    score = 0
    signals = {}

    if creator:
        creator_rows = [b for b in buyers if b.get("wallet_address") == creator]
        signals["creator_in_first_buyers"] = bool(creator_rows)
        if creator_rows:
            score += 15

    if not buyers:
        return {"wash_score": score, "signals": signals}

    from bot.models import execute, close_cursor, _scalar
    addrs = [b["wallet_address"] for b in buyers[:20]]

    # Serial flippers: wallets trading many distinct tokens are farm
    # routers; real early believers have concentrated histories.
    spread_counts = []
    for addr in addrs:
        c = execute(
            "SELECT COUNT(DISTINCT token_address) FROM wallet_buys "
            "WHERE wallet_address = %s", (addr,))
        spread_counts.append(_scalar(c) or 0)
        close_cursor(c)
    serial = sum(1 for n in spread_counts
                 if n >= WASH_SERIAL_FLIPPER_TOKENS)
    serial_ratio = serial / len(spread_counts)
    signals["serial_flipper_ratio"] = round(serial_ratio, 2)
    signals["serial_flag"] = serial_ratio >= 0.5
    if signals["serial_flag"]:
        score += 25

    # Buyer cluster: the same wallet set appearing together in the first
    # buyers of several prior tokens travels coin-to-coin = coordinated.
    if len(addrs) >= 10:
        c = execute("""
            SELECT COUNT(*) FROM (
                SELECT token_address FROM wallet_buys
                WHERE wallet_address = ANY(%s)
                GROUP BY token_address
                HAVING COUNT(DISTINCT wallet_address) >= 8
            ) AS shared_journeys
        """, (addrs,))
        shared = _scalar(c) or 0
        close_cursor(c)
        signals["shared_journeys"] = shared
        signals["cluster_flag"] = shared >= WASH_CLUSTER_MIN_JOURNEYS
        if signals["cluster_flag"]:
            score += 20

    return {"wash_score": score, "signals": signals}


# ── Public entry points ───────────────────────────────────────────────

def compute_wash_score(pair: dict, buyers: list[dict] | None = None,
                       rugcheck_report: dict | None = None) -> dict:
    """Full wash score for a candidate. Never raises — failed parts
    contribute 0 so calls never stall on the detector."""
    total, detail = 0, {}

    try:
        math_part = score_pair_math(pair)
        total += math_part["wash_score"]
        detail.update(math_part["signals"])
    except Exception:
        logger.exception("wash math signals failed")

    try:
        rc = score_rugcheck_creator(rugcheck_report)
        total += rc["wash_score"]
        detail.update(rc["signals"])
    except Exception:
        logger.exception("wash rugcheck-creator signals failed")

    if buyers:
        try:
            deep = score_first_buyers(buyers)
            total += deep["wash_score"]
            detail.update(deep["signals"])
        except Exception:
            logger.exception("wash buyer signals failed")

    return {"wash_score": min(100, total), "detail": detail}


def reject_call(wash_score: int) -> bool:
    """Owner decision: >= WASH_SCORE_MAX means NO CALL at all."""
    return wash_score >= WASH_SCORE_MAX


def allow_auto_buy(wash_score: int) -> bool:
    """Stricter than calls: the auto-trader only buys clean coins."""
    return wash_score < WASH_SCORE_AUTOBUY_MAX


def wash_line(wash_score: int, detail: dict) -> str:
    """Alert-card line: 🧼 Wash risk: LOW/MED/HIGH with the reasons."""
    if wash_score >= WASH_SCORE_MAX:
        level = "HIGH"
    elif wash_score >= 15:
        level = "MED"
    else:
        level = "LOW"
    bits = []
    if detail.get("cluster_flag"):
        bits.append("buyer cluster")
    if detail.get("serial_flag"):
        bits.append("serial flippers")
    if detail.get("symmetry_flag"):
        bits.append("flat churn")
    if detail.get("divergence_flag"):
        bits.append("fake volume")
    if detail.get("creator_in_first_buyers"):
        bits.append("creator trading")
    if detail.get("insider_networks"):
        bits.append(f"{detail['insider_networks']} insider nets")
    suffix = f" ({', '.join(bits)})" if bits else ""
    return f"🧼 Wash risk: {level} ({wash_score}/100){suffix}"
