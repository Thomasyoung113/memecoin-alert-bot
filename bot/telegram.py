"""
Telegram alert sender — formats and sends alerts to Telegram.
"""
import html
import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)


def _get_target_chat_ids() -> list[str]:
    """Return all chat IDs to send messages to."""
    ids = []
    if TELEGRAM_CHAT_ID:
        ids.append(TELEGRAM_CHAT_ID)
    if TELEGRAM_CHANNEL_ID:
        ids.append(TELEGRAM_CHANNEL_ID)
    return ids


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a plain message to all target chats. Returns True if at least one succeeded."""
    target_ids = _get_target_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not target_ids:
        logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN "
                       "and TELEGRAM_CHAT_ID in .env")
        return False

    success = False
    for chat_id in target_ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            success = True
        except requests.RequestException as e:
            logger.error("Failed to send to %s: %s", chat_id, e)
    return success


def _fmt_num(n: float) -> str:
    """Format a number nicely (K/M/B)."""
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.2f}"


def send_alert(token_address: str, symbol: str, mcap: float,
               price: float, snapshot: dict, safety: dict | None = None,
               smart_wallets: list[dict] | None = None):
    """
    Send a detailed alert about a promising token.

    Args:
        token_address: Token mint address
        symbol: Token symbol
        mcap: Market cap at alert time
        price: Price at alert time
        snapshot: Dict of metrics from the scanner
        safety: Dict from RugCheck checker
        smart_wallets: List of known smart wallets that bought early
    """
    short_addr = f"{token_address[:6]}...{token_address[-4:]}"
    chart_url = f"https://dexscreener.com/solana/{token_address}"
    target_2x = _fmt_num(mcap * 2)

    lines = [
        f"🚀 <b>${html.escape(symbol)}</b>",
        f"<code>{short_addr}</code>",
        f"",
        f"💰 MCap: {_fmt_num(mcap)}",
        f"🎯 Target (2x): {target_2x}",
        f"💧 Liq: {_fmt_num(snapshot.get('liquidity_usd', 0))}",
        f"📊 Vol(5m): {_fmt_num(snapshot.get('vol_5m', 0))}",
        f"🕐 Age: {snapshot.get('age_hours', '?')}h",
        f"📈 Price +{snapshot.get('price_change_h1', 0)}% (1h)",
        f"",
        f"🛒 Buys(24h): {snapshot.get('buys_24h', 0)}",
        f"💩 Sells(24h): {snapshot.get('sells_24h', 0)}",
        f"⚡ Txs(5m): {snapshot.get('txs_5m', 0)}",
    ]

    # RugCheck safety summary
    if safety:
        locked = safety.get("lp_locked_pct", 0)
        risk_count = safety.get("risk_count", 0)
        lock_icon = "🔒" if locked >= 100 else "⚠️"
        lines.append(f"")
        lines.append(f"{lock_icon} LP Locked: {locked:.0f}%")
        if risk_count > 0:
            lines.append(f"⚠️ Risks: {risk_count}")
        else:
            lines.append(f"✅ No risks detected")

    # Smart wallet activity
    if smart_wallets:
        lines.append(f"")
        lines.append(f"👛 <b>Smart Money Activity:</b>")
        for w in smart_wallets[:3]:
            rate = w.get("success_rate", 0)
            total = w.get("total_early_buys", 0)
            addr_short = f"{w['address'][:4]}...{w['address'][-4:]}"
            lines.append(
                f"  ✅ {addr_short} — {rate:.0f}% hit rate ({total} picks)"
            )

    # Stats footer
    from bot.models import get_stats
    total_calls, successful_calls, success_rate = get_stats()
    if total_calls > 0:
        lines.append(f"")
        lines.append(f"📊 Bot Track Record: {success_rate}% ({successful_calls}/{total_calls})")

    # Chart link
    lines.append(f"")
    lines.append(f"🔗 <a href='{chart_url}'>View on DexScreener</a>")

    text = "\n".join(lines)
    _send_message(text)


def send_update(message: str):
    """Send a plain text update (for resolved trades, learning results, etc.)."""
    _send_message(f"📡 <b>Bot Update</b>\n\n{message}")


def send_resolved(symbol: str, mcap: float, target_mcap: float,
                  hit_2x: bool, peak_mcap: float | None,
                  hit_loss: bool = False, current_mcap: float | None = None):
    """Notify when an alerted token resolves (hit 2x, hit -50% loss, or stale)."""
    target_str = f"${target_mcap:,.0f}" if target_mcap > 0 else "?"
    peak_str = f"${peak_mcap:,.0f}" if peak_mcap else "?"
    current_str = f"${current_mcap:,.0f}" if current_mcap else "?"

    if hit_loss:
        lines = [
            f"💀 <b>${html.escape(symbol)} — Stopped Out (-50%)</b>",
            f"Entry MCap: {_fmt_num(mcap)}",
            f"Current MCap: {current_str}",
            f"Peak MCap: {peak_str}",
            f"",
            f"Token lost 50%% of its value. Alert resolved as loss.",
        ]
    elif hit_2x:
        lines = [
            f"✅ <b>${html.escape(symbol)} — Resolved (Win)</b>",
            f"Entry MCap: {_fmt_num(mcap)}",
            f"Target (2x): {target_str}",
            f"Peak MCap: {peak_str}",
            f"",
            f"🎯 Hit 2x! Profit achieved.",
        ]
    else:
        lines = [
            f"❌ <b>${html.escape(symbol)} — Resolved (Missed)</b>",
            f"Entry MCap: {_fmt_num(mcap)}",
            f"Target (2x): {target_str}",
            f"Peak MCap: {peak_str}",
        ]
        fell_short = target_mcap - (peak_mcap or 0)
        lines.append(f"")
        lines.append(f"Missed 2x by ${fell_short:,.0f}" if fell_short > 0 else
                     f"Token did not reach target.")

    from bot.models import get_stats, get_loss_counts
    total_calls, successful_calls, success_rate = get_stats()
    loss_count = get_loss_counts()
    lines.append(f"")
    lines.append(f"📊 Bot Track Record: {success_rate}% ({successful_calls}/{total_calls})")
    if loss_count > 0:
        lines.append(f"💀 Losses: {loss_count}")

    _send_message("\n".join(lines))


def send_learning_update(iteration: int, changes: list[dict], new_rate: float):
    """Send an update about what the bot learned."""
    lines = [
        f"🧠 <b>Learning Update (Iteration {iteration})</b>",
        f"New success rate target: {new_rate:.1f}%",
        f"",
        f"<b>Adjustments made:</b>",
    ]
    for c in changes:
        icon = "✅" if c.get("improved") else "⚙️"
        lines.append(f"  {icon} {c['filter']}: {c['old']} → {c['new']}")
    _send_message("\n".join(lines))