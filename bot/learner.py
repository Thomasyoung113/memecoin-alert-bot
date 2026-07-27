"""
Self-learning engine — analyzes past outcomes and tunes filter thresholds.
"""
import logging
import json
from datetime import datetime, timezone

from config import (
    FILTERS_TO_TUNE, FILTER_ADJUSTMENT_FACTOR,
    MCAP_MIN, MCAP_MAX, VOL_5M_MIN,
    BUYS_24H_MIN, SELLS_24H_MIN, TXS_5M_MIN,
    MIN_CALLS_FOR_LEARN,
)
from bot.models import (
    get_conn, save_learning_entry, set_filter_config,
    get_all_filter_configs, get_filter_config,
)

logger = logging.getLogger(__name__)


def _get_default(name: str) -> float:
    """Get the default config value for a filter."""
    return {
        "mcap_min": float(MCAP_MIN),
        "mcap_max": float(MCAP_MAX),
        "vol_5m_min": float(VOL_5M_MIN),
        "buys_24h_min": float(BUYS_24H_MIN),
        "sells_24h_min": float(SELLS_24H_MIN),
        "txs_5m_min": float(TXS_5M_MIN),
    }.get(name, 0.0)


def _get_current_value(name: str) -> float:
    """Get current value from DB or config default."""
    db_val = get_filter_config(name)
    if db_val is not None:
        try:
            return float(db_val)
        except (ValueError, TypeError):
            pass
    return _get_default(name)


def analyze_success_by_filter() -> dict:
    """
    Analyze how each filter value correlates with success.

    Returns a dict like:
      {"mcap_min": {"total": N, "success": N, "rate": X},
       "vol_5m_min": {"total": N, "success": N, "rate": X},
       ...}
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT scan_snapshot, hit_2x, alert_mcap FROM alerts "
        "WHERE resolved = 1 AND scan_snapshot IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return {}

    results = {}
    for f in FILTERS_TO_TUNE:
        results[f] = {"total": 0, "success": 0}

    for r in rows:
        try:
            snap = json.loads(r["scan_snapshot"])
        except (json.JSONDecodeError, TypeError):
            continue
        hit = bool(r["hit_2x"])

        # Map each filter to its snapshot field
        checks = {
            "mcap_min": snap.get("mcap", 0) >= _get_current_value("mcap_min"),
            "mcap_max": snap.get("mcap", 0) <= _get_current_value("mcap_max"),
            "vol_5m_min": (snap.get("vol_5m", 0)
                           >= _get_current_value("vol_5m_min")),
            "buys_24h_min": (snap.get("buys_24h", 0)
                             >= _get_current_value("buys_24h_min")),
            "sells_24h_min": (snap.get("sells_24h", 0)
                              >= _get_current_value("sells_24h_min")),
            "txs_5m_min": (snap.get("txs_5m", 0)
                           >= _get_current_value("txs_5m_min")),
        }
        for f_name, passed in checks.items():
            results[f_name]["total"] += 1
            if passed and hit:
                results[f_name]["success"] += 1

    for f_name, data in results.items():
        if data["total"] > 0:
            data["rate"] = data["success"] / data["total"]
        else:
            data["rate"] = 0.0

    return results


def analyze_success_by_mcap_buckets() -> dict:
    """
    Group alerts into MCap buckets and find which range had highest success.
    Returns bucket results.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT alert_mcap, hit_2x FROM alerts WHERE resolved = 1"
    ).fetchall()
    conn.close()

    if not rows:
        return {}

    buckets = {}
    for r in rows:
        mcap = r["alert_mcap"]
        # Create $100k buckets
        bucket = int(mcap / 100_000) * 100_000
        if bucket not in buckets:
            buckets[bucket] = {"total": 0, "success": 0}
        buckets[bucket]["total"] += 1
        if r["hit_2x"]:
            buckets[bucket]["success"] += 1

    for b, data in buckets.items():
        data["rate"] = data["success"] / data["total"] if data["total"] > 0 else 0

    return buckets


def run_learning_cycle(iteration: int) -> list[dict]:
    """
    Run one learning cycle: analyze results, tune filters.

    Returns list of changes made: [{"filter": name, "old": X, "new": Y, "improved": bool}]
    """
    # Count total resolved calls
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 1"
    ).fetchone()[0]
    conn.close()

    if total < MIN_CALLS_FOR_LEARN:
        logger.info("Only %d calls resolved, need %d to learn. Skipping.",
                    total, MIN_CALLS_FOR_LEARN)
        return []

    changes = []

    # 1. Analyze MCap buckets to find the sweet spot
    mcap_buckets = analyze_success_by_mcap_buckets()
    if len(mcap_buckets) >= 2:
        # Find bucket with highest success rate that has at least 2 calls
        best_bucket = max(
            ((b, d) for b, d in mcap_buckets.items() if d["total"] >= 2),
            key=lambda x: x[1]["rate"],
            default=(None, None),
        )
        if best_bucket[0] is not None:
            bucket_val = best_bucket[0]
            old_min = _get_current_value("mcap_min")
            old_max = _get_current_value("mcap_max")

            # Narrow mcap range around the best bucket
            new_min = max(50_000, bucket_val - 100_000)
            new_max = bucket_val + 200_000

            if new_min != old_min or new_max != old_max:
                set_filter_config("mcap_min", str(new_min))
                set_filter_config("mcap_max", str(new_max))
                save_learning_entry(
                    iteration, "mcap_range",
                    f"{new_min}-{new_max}",
                    best_bucket[1]["total"],
                    best_bucket[1]["success"],
                    round(best_bucket[1]["rate"] * 100, 1),
                )
                changes.append({
                    "filter": "MCap Range",
                    "old": f"${old_min:,.0f}-${old_max:,.0f}",
                    "new": f"${new_min:,.0f}-${new_max:,.0f}",
                    "improved": best_bucket[1]["rate"] > 0.5,
                })
                logger.info("Tuned MCap range: %.0f-%.0f → %.0f-%.0f",
                            old_min, old_max, new_min, new_max)

    # 2. Tune volume filter
    vol_analysis = analyze_success_by_filter().get("vol_5m_min", {})
    if vol_analysis.get("total", 0) >= 5:
        old_val = _get_current_value("vol_5m_min")
        if vol_analysis["rate"] < 0.4:
            # Volume threshold too loose → tighten
            new_val = min(old_val * (1 + FILTER_ADJUSTMENT_FACTOR), 100_000)
            set_filter_config("vol_5m_min", str(new_val))
            save_learning_entry(iteration, "vol_5m_min", str(new_val),
                                vol_analysis["total"], vol_analysis["success"],
                                round(vol_analysis["rate"] * 100, 1))
            changes.append({
                "filter": "Vol(5m) Min",
                "old": f"${old_val:,.0f}",
                "new": f"${new_val:,.0f}",
                "improved": False,
            })
        elif vol_analysis["rate"] > 0.7 and old_val > 10_000:
            # High success, try loosening to catch more
            new_val = max(10_000, old_val * (1 - FILTER_ADJUSTMENT_FACTOR))
            set_filter_config("vol_5m_min", str(new_val))
            save_learning_entry(iteration, "vol_5m_min", str(new_val),
                                vol_analysis["total"], vol_analysis["success"],
                                round(vol_analysis["rate"] * 100, 1))
            changes.append({
                "filter": "Vol(5m) Min",
                "old": f"${old_val:,.0f}",
                "new": f"${new_val:,.0f}",
                "improved": True,
            })

    # 3. Tune buys filter
    buys_analysis = analyze_success_by_filter().get("buys_24h_min", {})
    if buys_analysis.get("total", 0) >= 5:
        old_val = _get_current_value("buys_24h_min")
        if buys_analysis["rate"] < 0.4:
            new_val = int(old_val * (1 + FILTER_ADJUSTMENT_FACTOR))
            set_filter_config("buys_24h_min", str(new_val))
            save_learning_entry(iteration, "buys_24h_min", str(new_val),
                                buys_analysis["total"], buys_analysis["success"],
                                round(buys_analysis["rate"] * 100, 1))
            changes.append({
                "filter": "Buys(24h) Min",
                "old": str(int(old_val)),
                "new": str(new_val),
                "improved": False,
            })
        elif buys_analysis["rate"] > 0.7 and old_val > 30:
            new_val = max(30, int(old_val * (1 - FILTER_ADJUSTMENT_FACTOR)))
            set_filter_config("buys_24h_min", str(new_val))
            changes.append({
                "filter": "Buys(24h) Min",
                "old": str(int(old_val)),
                "new": str(new_val),
                "improved": True,
            })

    return changes


def get_overall_success_rate() -> float:
    """Get the overall bot success rate from all resolved alerts."""
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 1"
    ).fetchone()[0]
    success = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 1 AND hit_2x = 1"
    ).fetchone()[0]
    conn.close()
    if total == 0:
        return 0.0
    return (success / total) * 100