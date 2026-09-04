"""
Telegram alert sender — formats and sends alerts to Telegram.
"""
import html
import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)

# ── Safe logging helper (strip bot token from URLs) ───────────────────
_TOKEN_PLACEHOLDER = "BOT_TOKEN_REDACTED"


def _safe_log_error(action: str, chat_id: str, exception: Exception):
    """Log a Telegram API error without exposing the bot token in the URL."""
    msg = str(exception)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in msg:
        msg = msg.replace(TELEGRAM_BOT_TOKEN, _TOKEN_PLACEHOLDER)
    logger.error("Failed to %s for %s: %s", action, chat_id, msg)


def _get_target_chat_ids() -> list[str]:
    """Return all chat IDs to send messages to."""
    ids = []
    if TELEGRAM_CHAT_ID:
        ids.append(TELEGRAM_CHAT_ID)
    if TELEGRAM_CHANNEL_ID:
        ids.append(TELEGRAM_CHANNEL_ID)
    return ids


def _post_api(method: str, payload: dict, file_field: str = None,
              file_bytes: bytes = None, chat_id=None) -> bool:
    """Single Bot API call with token-redacted error logging."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        if file_field and file_bytes is not None:
            resp = requests.post(url, files={file_field: ("card.png", file_bytes,
                                                          "image/png")},
                                 data=payload, timeout=20)
        else:
            resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        _safe_log_error(method, chat_id if chat_id is not None else "broadcast", e)
    return False


def send_to_user(telegram_id: int, text: str, parse_mode: str = "HTML",
                 reply_markup: dict = None) -> bool:
    """DM one user. The per-user channel for alerts, receipts, reminders."""
    if not telegram_id:
        return False
    payload = {"chat_id": telegram_id, "text": text, "parse_mode": parse_mode,
               "disable_web_page_preview": False}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post_api("sendMessage", payload, chat_id=telegram_id)


def _send_message(text: str, parse_mode: str = "HTML",
                  reply_markup: dict = None) -> bool:
    """Send a plain message to all target chats. Returns True if at least one succeeded."""
    target_ids = _get_target_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not target_ids:
        logger.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN "
                       "and TELEGRAM_CHAT_ID in .env")
        return False

    success = False
    for chat_id in target_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if _post_api("sendMessage", payload, chat_id=chat_id):
            success = True
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


def build_alert_text(token_address: str, symbol: str, mcap: float,
                     snapshot: dict, safety: dict | None = None,
                     smart_wallets: list[dict] | None = None) -> str:
    """The full alert message body (shared by broadcast and per-user fanout)."""
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

    return "\n".join(lines)


def send_alert(token_address: str, symbol: str, mcap: float,
               price: float, snapshot: dict, safety: dict | None = None,
               smart_wallets: list[dict] | None = None,
               wash_score: int = 0, wash_detail: dict | None = None):
    """Broadcast the alert to the owner (+channel) AND DM eligible users."""
    from bot.models import get_telegram_ids_by_tier
    text = build_alert_text(token_address, symbol, mcap, snapshot, safety,
                            smart_wallets)
    if wash_detail is not None:
        from bot.wash_detector import wash_line
        lines = text.split("\n")
        # insert the wash line right after the RugCheck block (before the
        # track-record footer): find the chart link line
        for i, line in enumerate(lines):
            if line.startswith("🔗"):
                lines.insert(i, wash_line(wash_score, wash_detail))
                lines.insert(i + 1, "")
                break
        text = "\n".join(lines)
    _send_message(text)
    # Per-user fanout: trial + paid Pro get every call in real time.
    # (Free tier only gets the weekly top call — see billing jobs.)
    for tid in get_telegram_ids_by_tier(["trial", "pro"],
                                        statuses=("active", "grace")):
        if tid != TELEGRAM_CHAT_ID:  # owner already got the broadcast
            send_to_user(tid, text)


def send_update(message: str):
    """Send a plain text update (for resolved trades, learning results, etc.)."""
    _send_message(f"📡 <b>Bot Update</b>\n\n{html.escape(message)}")


def notify_owner(text: str):
    """DM the owner only (TELEGRAM_CHAT_ID) — per-user trade confirmations
    and money-path alerts must not leak to a public channel."""
    if TELEGRAM_CHAT_ID:
        try:
            _post_api("sendMessage", {"chat_id": int(TELEGRAM_CHAT_ID),
                                      "text": text, "parse_mode": "HTML"},
                      chat_id=TELEGRAM_CHAT_ID)
        except (ValueError, TypeError):
            _post_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID,
                                      "text": text, "parse_mode": "HTML"},
                      chat_id=TELEGRAM_CHAT_ID)


def send_pro_update(message: str):
    """Pro-only feature alerts (whale movements, insider selling)."""
    from bot.models import get_telegram_ids_by_tier
    text = f"🐋 <b>Pro Alert</b>\n\n{html.escape(message)}"
    _send_message(text)  # owner + channel
    for tid in get_telegram_ids_by_tier(["pro", "trial"],
                                        statuses=("active", "grace")):
        if str(tid) != str(TELEGRAM_CHAT_ID):
            send_to_user(tid, text)


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


def send_photo(photo_bytes: bytes, caption: str = None,
               telegram_id: int = None):
    """Send a photo (with a proper photo caption) to target chats or one user."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    if telegram_id:
        target_ids = [telegram_id]
    else:
        target_ids = _get_target_chat_ids()
    if not target_ids:
        return False
    success = False
    for chat_id in target_ids:
        payload = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        if _post_api("sendPhoto", payload, file_field="photo",
                     file_bytes=photo_bytes, chat_id=chat_id):
            success = True
    return success


def send_pnl_card(symbol: str, pnl_pct: float, entry_mcap: float,
                   current_mcap: float = None, peak_mcap: float = None,
                   duration: str = None, wallet: str = None,
                   caption_title: str = None, multiplier: float = None,
                   sol_spent: float = None, sol_received: float = None,
                   telegram_id: int = None):
    """Generate and send a GEMBOT-branded PnL card as a photo."""
    is_win = pnl_pct >= 0
    from bot.pnl_card import generate_pnl_card
    img_bytes = generate_pnl_card(
        token_symbol=symbol,
        pnl_pct=pnl_pct,
        entry_mcap=entry_mcap,
        current_mcap=current_mcap,
        peak_mcap=peak_mcap,
        duration=duration,
        wallet=wallet,
        telegram_username="thomasgem",
        is_win=is_win,
        multiplier=multiplier,
        sol_spent=sol_spent,
        sol_received=sol_received,
    )
    status = caption_title or ("✅ HIT 2x!" if is_win else "💀 STOPPED OUT")
    caption = (
        f"<b>{status}</b>\n"
        f"${html.escape(symbol)} | GEMBOT"
    )
    send_photo(img_bytes, caption=caption, telegram_id=telegram_id)


def send_milestone_card(symbol: str, multiplier: float, alert_mcap: float,
                        current_mcap: float = None, duration: str = None,
                        telegram_id: int = None):
    """Send a celebration card when a bot call crosses a new multiple (1.5x, 2x, 3x...)."""
    from bot.pnl_card import generate_pnl_card
    pnl_pct = (multiplier - 1) * 100
    img_bytes = generate_pnl_card(
        token_symbol=symbol,
        pnl_pct=pnl_pct,
        entry_mcap=alert_mcap,
        current_mcap=current_mcap,
        peak_mcap=current_mcap,
        duration=duration,
        telegram_username="thomasgem",
        is_win=True,
        multiplier=multiplier,
        milestone_mode=True,
    )
    caption = (
        f"🚀 <b>${html.escape(symbol)} just hit {multiplier:g}x</b> from a GEMBOT call!"
    )
    send_photo(img_bytes, caption=caption, telegram_id=telegram_id)


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