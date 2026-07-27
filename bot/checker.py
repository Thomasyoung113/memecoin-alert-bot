"""
RugCheck safety checker — verifies token safety before alerting.
"""
import logging

import requests

from config import RUGCHECK_BASE, LIQUIDITY_LOCKED_TARGET

logger = logging.getLogger(__name__)


def check_token(token_address: str) -> dict | None:
    """
    Run RugCheck on a token address.

    Returns a dict with safety info, or None if the check failed.
    """
    url = f"{RUGCHECK_BASE}/tokens/{token_address}/report/summary"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            logger.debug("RugCheck: no report yet for %s", token_address[:8])
            return None
        resp.raise_for_status()
        data = resp.json()
        return data
    except requests.RequestException as e:
        logger.warning("RugCheck request failed for %s: %s",
                       token_address[:8], e)
        return None


def is_safe(token_address: str) -> tuple[bool, dict]:
    """
    Check if a token passes the RugCheck safety checks.

    Returns (is_safe, details_dict).
    """
    report = check_token(token_address)
    if report is None:
        # No report yet — we can't confirm safety, treat as unsafe
        return False, {"reason": "no_report"}

    risks = report.get("risks", [])
    lp_locked = report.get("lpLockedPct", 0)
    score = report.get("score", 0)

    details = {
        "lp_locked_pct": lp_locked,
        "score": score,
        "risks": [r.get("name", str(r)) if isinstance(r, dict) else str(r)
                  for r in risks],
        "risk_count": len(risks),
    }

    # Check LP lock
    if lp_locked < LIQUIDITY_LOCKED_TARGET:
        logger.info("UNSAFE %s: LP locked %.0f%% < %d%%",
                    token_address[:8], lp_locked, LIQUIDITY_LOCKED_TARGET)
        return False, {**details, "reason": "lp_not_locked"}

    # Check for critical risks
    if len(risks) > 0:
        # Some risks are informational; flag anything non-zero for safety
        critical = [r for r in risks
                    if isinstance(r, dict) and r.get("level") == "critical"]
        if critical:
            logger.info("UNSAFE %s: %d critical risks",
                        token_address[:8], len(critical))
            return False, {**details, "reason": "critical_risks"}

    logger.info("SAFE %s: LP=%.0f%%, risks=%d, score=%d",
                token_address[:8], lp_locked, len(risks), score)
    return True, details