"""
Telegram listener — polls for user commands and dispatches them.
Supports multi-user wallet management (Phase 1) + auto-trading (Phase 3).

Commands:
  /start              — Create profile + auto-generate first wallet
  /wallet             — Show current default wallet info
  /wallet new <label> — Generate a new wallet with a label
  /wallet list        — List all wallets
  /wallet default <label> — Set default wallet
  /wallet export      — ⚠️ Export private key (use with caution)
  /balance            — Show SOL + token balances
  /buy <addr> <sol>   — Instant buy via Jupiter
  /sell <addr> [pct]  — Instant sell + PnL card
  /slippage <bps>     — Set swap slippage
  /status <sig>       — Check transaction status
  /auto [on|off|set]  — Auto-trading on bot calls

Runs in a background daemon thread alongside the main scanner.
"""
import html
import logging
import os
import time
import threading
import re

import requests

import base58

from config import TELEGRAM_BOT_TOKEN
from bot.models import (
    get_or_create_user, get_user_by_telegram_id,
    get_user_wallets, get_default_wallet, create_user_wallet,
    set_default_wallet, get_wallet_by_label,
    save_trade, get_user_trades, get_trade_by_sig, update_trade_status,
    get_alert_for_token, get_last_buy_trade,
)
from bot.wallet import generate_wallet, get_wallet_info, decrypt_private_key
from bot.jupiter import buy_token, sell_token, get_quote, check_transaction_status, get_token_decimals
from bot.helius import SOLANA_RPC
from bot.telegram import send_pnl_card

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_POLL_INTERVAL = 3
_OFFSET_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "tg_offset.txt")

# ── Rate limiting ─────────────────────────────────────────────────────
_RATE_LIMIT = {}  # chat_id -> [timestamp, ...]


def _check_rate_limit(chat_id: str, max_per_minute: int = 5) -> bool:
    """Return True if the message is allowed (under rate limit)."""
    now = time.time()
    recent = [t for t in _RATE_LIMIT.get(chat_id, []) if now - t < 60]
    if len(recent) >= max_per_minute:
        return False
    recent.append(now)
    _RATE_LIMIT[chat_id] = recent
    return True


# ── Helpers ───────────────────────────────────────────────────────────

def _reply(chat_id: int, text: str, parse_mode: str = "HTML",
           reply_markup: dict = None):
    """Send a reply message to a chat."""
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(f"{_API}/sendMessage", json=payload, timeout=10)
    except requests.RequestException as e:
        logger.debug("Failed to reply to %s: %s", chat_id, e)


def _short_pubkey(pubkey: str) -> str:
    if len(pubkey) > 12:
        return f"{pubkey[:6]}...{pubkey[-4:]}"
    return pubkey


def _fmt_sol(lamports_or_sol: float, is_lamports: bool = False) -> str:
    sol = lamports_or_sol / 1_000_000_000 if is_lamports else lamports_or_sol
    if sol >= 10:
        return f"◎ {sol:.2f}"
    if sol >= 1:
        return f"◎ {sol:.4f}"
    if sol >= 0.001:
        return f"◎ {sol:.6f}"
    return f"◎ {sol:.9f}"


# ── Command handlers ──────────────────────────────────────────────────

def _cmd_start(chat_id: int, text: str):
    """Join gate → create user + first wallet → trial."""
    from bot import billing
    from bot.models import ensure_subscription, get_subscription

    # ── Join gate: must be a member of the gate channel ──────────────
    if not billing.is_channel_member(chat_id):
        _reply(chat_id, billing.join_gate_message(),
               reply_markup=billing.join_gate_markup())
        return

    user = get_or_create_user(chat_id)
    wallets = get_user_wallets(user["id"])
    if not wallets:
        try:
            pubkey, encrypted, _ = generate_wallet()
            wallet = create_user_wallet(user["id"], "main", pubkey, encrypted)
        except RuntimeError as e:
            _reply(chat_id, f"❌ {html.escape(str(e))}")
            return
        # Start the 3-day full-access trial on first entry
        sub = billing.start_trial(user["id"])
        from bot.billing import subscription_status_line
        _reply(chat_id,
            "🚀 <b>Welcome to GEMBOT!</b>\n\n"
            f"✅ Your Solana wallet:\n<code>{pubkey}</code>\n\n"
            f"{subscription_status_line(user['id'])}\n"
            "⚡ For your trial you get EVERY call in real time + 24/7 auto-trading.\n\n"
            "🤖 <b>Quick start:</b>\n"
            "  /auto — arm auto-trading (amount, TP/SL)\n"
            "  /positions — track open trades\n"
            "  /wallet — wallet info · /balance — balances\n\n"
            f"💎 After the trial: <b>0.2 SOL/week · 0.5 SOL/month</b>\n"
            "Free tier keeps 1 top call/week + your wallet.\n"
            "🎟 Promo codes drop daily on @thomas_young",
            reply_markup={"inline_keyboard": [
                [{"text": "🤖 Arm Auto-Trading", "callback_data": "auto:menu"}],
                [{"text": "💎 Go Pro", "callback_data": "sub:menu"}],
            ]})
    else:
        default = get_default_wallet(user["id"])
        addr = _short_pubkey(default["public_key"]) if default else "?"
        from bot.billing import subscription_status_line
        ensure_subscription(user["id"])
        _reply(chat_id,
            "👋 <b>Welcome back!</b>\n\n"
            f"Default wallet: {addr}\n"
            f"{subscription_status_line(user['id'])}\n\n"
            "Use /positions, /auto or /balance.",
            reply_markup={"inline_keyboard": [
                [{"text": "📊 Positions", "callback_data": "pos:list"},
                 {"text": "💎 Go Pro", "callback_data": "sub:menu"}],
            ]})


def _cmd_wallet(chat_id: int, text: str):
    """Handle /wallet subcommands."""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first to create your profile.")
        return

    # Parse subcommand
    parts = text.strip().split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "show"

    if sub == "show" or sub == "info":
        default = get_default_wallet(user["id"])
        if not default:
            _reply(chat_id, "❌ No wallets found. Use /start to create one.")
            return
        info = get_wallet_info(default["public_key"])
        sol_str = _fmt_sol(info["sol"])
        token_count = len(info["tokens"])
        _reply(chat_id,
            f"🏦 <b>Wallet: {html.escape(default['label'])}</b>\n"
            f"<code>{default['public_key']}</code>\n\n"
            f"💰 {sol_str}\n"
            f"📦 {token_count} token(s)\n\n"
            f"Use /balance for full details."
        )

    elif sub == "new":
        label = parts[2].strip().lower() if len(parts) > 2 else ""
        if not label or not re.match(r'^[a-z0-9_-]{1,20}$', label):
            _reply(chat_id, "❌ Usage: /wallet new &lt;label&gt;\nLabel: 1-20 chars, letters/numbers/-/_")
            return
        existing = get_wallet_by_label(user["id"], label)
        if existing:
            _reply(chat_id, f"❌ You already have a wallet named '{html.escape(label)}'.")
            return
        try:
            pubkey, encrypted, _ = generate_wallet()
            wallet = create_user_wallet(user["id"], label, pubkey, encrypted)
            _reply(chat_id,
                f"✅ New wallet '<b>{html.escape(label)}</b>' created!\n"
                f"<code>{pubkey}</code>"
            )
        except RuntimeError as e:
            _reply(chat_id, f"❌ {e}")

    elif sub == "list":
        wallets = get_user_wallets(user["id"])
        if not wallets:
            _reply(chat_id, "❌ No wallets found. Use /start to create one.")
            return
        lines = ["📋 <b>Your Wallets</b>"]
        for w in wallets:
            marker = "✅ " if w["is_default"] else "   "
            lines.append(f"{marker}<b>{html.escape(w['label'])}</b>: {_short_pubkey(w['public_key'])}")
        _reply(chat_id, "\n".join(lines))

    elif sub == "default":
        if len(parts) < 3:
            _reply(chat_id, "❌ Usage: /wallet default &lt;label&gt;")
            return
        label = parts[2].strip().lower()
        wallet = get_wallet_by_label(user["id"], label)
        if not wallet:
            _reply(chat_id, f"❌ No wallet named '{html.escape(label)}'.")
            return
        set_default_wallet(user["id"], wallet["id"])
        _reply(chat_id, f"✅ Default wallet set to '<b>{html.escape(label)}</b>'.")

    elif sub == "export":
        # Require confirmation: /wallet export confirm
        if len(parts) < 3 or parts[2].strip().lower() != "confirm":
            _reply(chat_id,
                "⚠️ <b>Export Private Key</b>\n\n"
                "This will expose your wallet's private key. "
                "Anyone with this key can control your wallet.\n\n"
                "To confirm, send:\n"
                "<code>/wallet export confirm</code>"
            )
            return
        default = get_default_wallet(user["id"], include_privkey=True)
        if not default:
            _reply(chat_id, "❌ No wallets to export.")
            return
        from bot.wallet import decrypt_private_key
        try:
            raw_key = decrypt_private_key(default["encrypted_private_key"])
            # Encode full 64-byte secret key as base58 (standard Solana format)
            priv_b58 = base58.b58encode(raw_key).decode()
            logger.info("Wallet export by user %s (wallet: %s)", chat_id, _short_pubkey(default["public_key"]))
            _reply(chat_id,
                "⚠️ <b>PRIVATE KEY EXPORT</b>\n\n"
                f"Wallet: <b>{html.escape(default['label'])}</b>\n"
                f"Address: <code>{default['public_key']}</code>\n\n"
                f"Private key (base58):\n"
                f"<code>{html.escape(priv_b58)}</code>\n\n"
                "🔴 <b>Never share this with anyone.</b>\n"
                "Delete this message after saving."
            )
        except Exception as e:
            logger.info("Export failed for user %s: wallet %s", chat_id, _short_pubkey(default["public_key"]))
            _reply(chat_id, "❌ Failed to export private key.")

    else:
        _reply(chat_id,
            "❌ Unknown subcommand. Try: /wallet, /wallet new &lt;label&gt;, /wallet list, /wallet default &lt;label&gt;, /wallet export confirm")


def _cmd_balance(chat_id: int, text: str):
    """Show SOL + token balances for the default wallet."""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first to create your profile.")
        return
    default = get_default_wallet(user["id"])
    if not default:
        _reply(chat_id, "❌ No wallets found. Use /start to create one.")
        return
    info = get_wallet_info(default["public_key"])
    sol_str = _fmt_sol(info["sol"])
    lines = [
        f"💰 <b>Balance</b>",
        f"Wallet: {_short_pubkey(default['public_key'])}",
        f"",
        f"{sol_str}",
    ]
    if info["tokens"]:
        lines.append(f"")
        lines.append(f"<b>Tokens:</b>")
        for t in info["tokens"][:10]:
            mint_short = _short_pubkey(t["mint"])
            # Fetch symbol from DexScreener? For now show mint
            lines.append(f"  {t['amount']:.2f} — <code>{mint_short}</code>")
    else:
        lines.append(f"")
        lines.append(f"No SPL tokens found.")
    _reply(chat_id, "\n".join(lines))


# ── Trading commands (Phase 2) ────────────────────────────────────────

def _cmd_buy(chat_id: int, text: str):
    """Buy tokens: /buy <token_address> <amount_sol> — instant swap. Pro only."""
    if not _require_pro(chat_id):
        return
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first to create your profile.")
        return
    parts = text.strip().split()
    if len(parts) < 3:
        _reply(chat_id, "❌ Usage: /buy &lt;token_address&gt; &lt;amount_sol&gt;\nExample: /buy So11111111111111111111111111111111111111112 0.5")
        return
    token_address = parts[1].strip()
    if len(token_address) < 32 or not token_address.isalnum():
        _reply(chat_id, "❌ Invalid token address.")
        return
    try:
        amount_sol = float(parts[2])
    except ValueError:
        _reply(chat_id, "❌ Amount must be a number (e.g. 0.5)")
        return
    if amount_sol <= 0 or amount_sol > 100:
        _reply(chat_id, "❌ Amount must be between 0.001 and 100 SOL.")
        return

    wallet = get_default_wallet(user["id"], include_privkey=True)
    if not wallet:
        _reply(chat_id, "❌ No wallet found. Use /start to create one.")
        return

    # Quote for preview + price sanity
    from bot.jupiter import SOL_MINT, LAMPORTS_PER_SOL, get_token_decimals
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    quote = get_quote(SOL_MINT, token_address, amount_lamports,
                      int(wallet.get("slippage_bps") or 500))
    if not quote:
        _reply(chat_id, "❌ Could not get a quote for this token. Check the address.")
        return

    out_amount = int(quote.get("outAmount", 0))
    price_impact = float(quote.get("priceImpactPct", 0))

    if price_impact > 15:
        _reply(chat_id,
               f"❌ Price impact too high: {price_impact:.1f}% — trade aborted.")
        return

    _reply(chat_id, "⏳ Executing instant buy...")
    try:
        raw_key = decrypt_private_key(wallet["encrypted_private_key"])
        sig = buy_token(
            token_address=token_address,
            amount_sol=amount_sol,
            user_pubkey=wallet["public_key"],
            keypair_bytes=raw_key,
            slippage_bps=int(wallet.get("slippage_bps") or 500),
        )
    except Exception as e:
        logger.error("Buy failed for user %s: %s", chat_id, e)
        sig = None

    if not sig:
        _reply(chat_id, "❌ Swap failed. Check token address and try again.")
        return

    # Record the trade so /sell can compute PnL
    alert = get_alert_for_token(token_address)
    symbol = alert["symbol"] if alert else token_address[:8]
    decimals = get_token_decimals(token_address)
    amount_token = out_amount / (10 ** decimals) if out_amount else None
    entry_price_sol = None
    if out_amount:
        entry_price_sol = (amount_lamports / LAMPORTS_PER_SOL) / (out_amount / (10 ** decimals))
    save_trade(user["id"], wallet["id"], "buy", token_address, token_symbol=symbol,
               amount_sol=amount_sol, amount_token=amount_token,
               price_sol=entry_price_sol, tx_signature=sig,
               slippage_bps=int(wallet.get("slippage_bps") or 500))

    _reply(chat_id,
        f"✅ <b>Buy Executed!</b>\n\n"
        f"Token: ${html.escape(symbol)}\n"
        f"Spent: ◎ {amount_sol:.4f}\n"
        f"Received: {out_amount:,} raw units\n"
        f"Tx: <code>{sig[:16]}...</code>\n\n"
        f"🔗 <a href='https://solscan.io/tx/{sig}'>View on Solscan</a>"
    )


def _cmd_sell(chat_id: int, text: str):
    """Sell tokens: /sell <token_address> [pct%]"""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first to create your profile.")
        return
    parts = text.strip().split()
    if len(parts) < 2:
        _reply(chat_id, "❌ Usage: /sell &lt;token_address&gt; [percentage=100]\nExample: /sell EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 50")
        return
    token_address = parts[1].strip()
    if len(token_address) < 32:
        _reply(chat_id, "❌ Invalid token address.")
        return

    # Parse percentage (default 100%)
    pct = 100.0
    if len(parts) >= 3:
        try:
            pct = float(parts[2])
        except ValueError:
            _reply(chat_id, "❌ Percentage must be a number (e.g. 50).")
            return
    if pct <= 0 or pct > 100:
        _reply(chat_id, "❌ Percentage must be between 1 and 100.")
        return

    wallet = get_default_wallet(user["id"], include_privkey=True)
    if not wallet:
        _reply(chat_id, "❌ No wallet found.")
        return

    # Get token balance
    from bot.wallet import get_token_balances
    tokens = get_token_balances(wallet["public_key"])
    token_info = None
    for t in tokens:
        if t["mint"] == token_address:
            token_info = t
            break
    if not token_info:
        _reply(chat_id, "❌ No balance found for this token in your wallet.")
        return

    amount = int(token_info["amount"] * (pct / 100) * (10 ** token_info["decimals"]))

    _reply(chat_id, f"⏳ Selling {pct}% of token...")

    slippage_bps = int(wallet.get("slippage_bps") or 500)
    try:
        raw_key = decrypt_private_key(wallet["encrypted_private_key"])
        sig = sell_token(
            token_address=token_address,
            amount_tokens=amount,
            user_pubkey=wallet["public_key"],
            keypair_bytes=raw_key,
            slippage_bps=slippage_bps,
        )
    except Exception as e:
        logger.error("Sell failed for user %s: %s", chat_id, e)
        sig = None

    if not sig:
        _reply(chat_id, "❌ Sell failed.")
        return

    save_trade(user["id"], wallet["id"], "sell", token_address,
               token_symbol=token_info.get("symbol"),
               amount_token=token_info["amount"], tx_signature=sig,
               slippage_bps=slippage_bps)

    _reply(chat_id,
        f"✅ <b>Sell Executed!</b>\n\n"
        f"Sold: {pct}%\n"
        f"Tx: <code>{sig[:16]}...</code>\n\n"
        f"🔗 <a href='https://solscan.io/tx/{sig}'>View on Solscan</a>"
    )

    # ── PnL card: entry from last recorded buy, exit from this tx ────
    try:
        from bot.auto_trader import _fetch_sol_received
        from bot.tracker import _fetch_current_mcap
        sol_received = _fetch_sol_received(sig)
        buy = get_last_buy_trade(user["id"], token_address)
        entry_price_sol = buy.get("price_sol") if buy else None
        pnl = None
        if entry_price_sol:
            from bot.jupiter import SOL_MINT, get_quote
            exit_quote = get_quote(token_address, SOL_MINT, max(1, amount // 1000),
                                   slippage_bps)
            if exit_quote:
                exit_price_sol = (int(exit_quote.get("outAmount", 0)) / 1e9) \
                    / max(1, (amount / (10 ** token_info["decimals"])) / 1000)
                pnl = (exit_price_sol / entry_price_sol - 1) * 100
        alert = get_alert_for_token(token_address)
        if pnl is None and alert and alert.get("alert_mcap"):
            current_mcap = _fetch_current_mcap(token_address)
            if current_mcap:
                pnl = (current_mcap / alert["alert_mcap"] - 1) * 100
        if pnl is not None:
            multiplier = (alert["alert_mcap"] and _fetch_current_mcap(token_address)
                          and _fetch_current_mcap(token_address) / alert["alert_mcap"]) \
                          if alert else None
            send_pnl_card(
                symbol=token_info.get("symbol") or token_address[:8],
                pnl_pct=pnl,
                entry_mcap=alert["alert_mcap"] if alert else None,
                current_mcap=_fetch_current_mcap(token_address),
                duration=None,
                wallet=_short_pubkey(wallet["public_key"]),
                caption_title=f"💰 SOLD — {pnl:+.1f}%",
                multiplier=multiplier,
                sol_spent=buy.get("amount_sol") if buy else None,
                sol_received=sol_received,
            )
    except Exception as e:
        logger.error("Sell PnL card failed: %s", e)


def _cmd_slippage(chat_id: int, text: str):
    """Set slippage: /slippage <bps>"""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first.")
        return
    parts = text.strip().split()
    if len(parts) < 2:
        _reply(chat_id, "❌ Usage: /slippage &lt;bps&gt;\nExample: /slippage 500 (5%)")
        return
    try:
        bps = int(parts[1])
    except ValueError:
        _reply(chat_id, "❌ Slippage must be a number in basis points (100 = 1%, 500 = 5%).")
        return
    if bps < 50 or bps > 5000:
        _reply(chat_id, "❌ Slippage must be between 50 (0.5%) and 5000 (50%).")
        return
    wallet = get_default_wallet(user["id"])
    if not wallet:
        _reply(chat_id, "❌ No wallet found.")
        return
    # Store slippage in DB — add column or use a simple user_data approach
    from bot.models import execute, close_cursor, commit
    c = execute("UPDATE user_wallets SET slippage_bps = %s WHERE id = %s",
                (bps, wallet["id"]))
    close_cursor(c)
    commit()
    _reply(chat_id, f"✅ Slippage set to {bps / 100:.1f}% ({bps} bps).")


def _cmd_status(chat_id: int, text: str):
    """Check tx status: /status <tx_signature>"""
    parts = text.strip().split()
    if len(parts) < 2:
        _reply(chat_id, "❌ Usage: /status &lt;tx_signature&gt;")
        return
    sig = parts[1].strip()
    _reply(chat_id, "⏳ Checking transaction status...")
    result = check_transaction_status(sig)
    if result["confirmed"]:
        _reply(chat_id,
            f"✅ <b>Transaction Confirmed</b>\n\n"
            f"Signature: <code>{sig[:20]}...</code>\n"
            f"Slot: {result['slot']}\n\n"
            f"🔗 <a href='https://solscan.io/tx/{sig}'>View on Solscan</a>"
        )
    elif result["error"]:
        _reply(chat_id, f"❌ Transaction failed: {result['error']}")
    else:
        _reply(chat_id, "⏳ Transaction still pending...")


# ── Auto-trading commands (Phase 3) ───────────────────────────────────

def _auto_menu_markup(cfg: dict | None) -> dict:
    """Inline menu for /auto: amount presets + TP/SL presets + power."""
    amount = float((cfg or {}).get("buy_amount_sol") or 0.1)
    enabled = bool((cfg or {}).get("is_enabled"))
    amt_row = []
    for preset in (0.05, 0.1, 0.25, 0.5, 1.0):
        label = f"◎{preset:g}" + (" ✅" if abs(amount - preset) < 1e-9 else "")
        amt_row.append({"text": label, "callback_data": f"auto:amt_{preset:g}"})
    return {"inline_keyboard": [
        amt_row,
        [{"text": f"TP +100%", "callback_data": "auto:tp_100"},
         {"text": "TP +200%", "callback_data": "auto:tp_200"},
         {"text": "SL -50%", "callback_data": "auto:sl_50"},
         {"text": "SL -40%", "callback_data": "auto:sl_40"}],
        [{"text": "🤖 Arm ON" if not enabled else "⭕ Turn OFF",
          "callback_data": "auto:toggle"},
         {"text": "🔄 Refresh", "callback_data": "auto:menu"}],
    ]}


def _cmd_auto(chat_id: int, text: str):
    """Auto-trading: /auto — show status, /auto on|off, /auto set <key> <value>."""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first to create your profile.")
        return
    wallet = get_default_wallet(user["id"])
    if not wallet:
        _reply(chat_id, "❌ No wallet found. Use /start to create one.")
        return

    from bot.models import get_auto_trade_config, upsert_auto_trade_config

    parts = text.strip().split()
    cfg = get_auto_trade_config(user["id"], wallet["id"])

    # /auto — show current config + open positions
    if len(parts) == 1:
        from bot.models import get_open_positions
        positions = [p for p in get_open_positions()
                     if p["user_id"] == user["id"] and p["wallet_id"] == wallet["id"]]
        lines = ["🤖 <b>Auto-Trading</b>"]
        if cfg and cfg["is_enabled"]:
            lines.append("Status: ✅ <b>ON</b>")
        else:
            lines.append("Status: ⭕ OFF")
        lines.append("")
        lines.append(f"Buy amount: ◎ {cfg['buy_amount_sol'] if cfg else 0.1:.4f}")
        lines.append(f"Max positions: {cfg['max_positions'] if cfg else 3}")
        lines.append(f"Take profit: +{cfg['take_profit_pct'] if cfg else 100:.0f}%")
        lines.append(f"Stop loss: -{cfg['stop_loss_pct'] if cfg else 50:.0f}%")
        lines.append(f"Cooldown: {cfg['cooldown_minutes'] if cfg else 0}m")
        lines.append("")
        lines.append(f"Open positions: {len(positions)}")
        for p in positions[:5]:
            pnl = p.get("pnl_pct")
            lines.append(f"  • ${p.get('token_symbol') or p['token_address'][:6]}"
                         f" — ◎{p.get('amount_sol_invested') or 0:.4f} in")
        lines.append("")
        lines.append("<b>Commands:</b>")
        lines.append("<code>/auto on</code> — enable")
        lines.append("<code>/auto off</code> — disable")
        lines.append("<code>/auto set amount 0.25</code> — SOL per buy")
        lines.append("<code>/auto set maxpos 5</code>")
        lines.append("<code>/auto set tp 200</code> — take profit %")
        lines.append("<code>/auto set sl 40</code> — stop loss %")
        lines.append("<code>/auto set cooldown 30</code> — minutes")
        _reply(chat_id, "\n".join(lines),
               reply_markup=_auto_menu_markup(cfg))
        return

    sub = parts[1].lower()

    if sub == "on":
        if not _require_pro(chat_id):
            return
        cfg = upsert_auto_trade_config(user["id"], wallet["id"],
                                       {"is_enabled": True})
        _reply(chat_id,
               "✅ <b>Auto-trading ON</b>\n\n"
               f"Buy: ◎ {cfg['buy_amount_sol']:.4f} per call\n"
               f"TP +{cfg['take_profit_pct']:.0f}% / SL -{cfg['stop_loss_pct']:.0f}%\n"
               f"Max {cfg['max_positions']} positions, {cfg['cooldown_minutes']}m cooldown\n\n"
               "⚡ Instant-buy fires on every bot call.\n"
               "Sell cards + milestone cards will follow.")
        return

    if sub == "off":
        upsert_auto_trade_config(user["id"], wallet["id"], {"is_enabled": False})
        _reply(chat_id, "⭕ <b>Auto-trading OFF</b>\n\nOpen positions are still "
                       "monitored for TP/SL until you /sell them.")
        return

    if sub == "set":
        if not _require_pro(chat_id):
            return
        if len(parts) < 4:
            _reply(chat_id,
                   "❌ Usage: /auto set &lt;key&gt; &lt;value&gt;\n"
                   "Keys: amount, maxpos, tp, sl, cooldown")
            return
        key, value = parts[2].lower(), parts[3]
        mapping = {
            "amount": ("buy_amount_sol", 0.001, 10.0),
            "maxpos": ("max_positions", 1, 20),
            "tp": ("take_profit_pct", 10, 10000),
            "sl": ("stop_loss_pct", 1, 99),
            "cooldown": ("cooldown_minutes", 0, 1440),
        }
        if key not in mapping:
            _reply(chat_id, "❌ Unknown key. Use: amount, maxpos, tp, sl, cooldown")
            return
        col, lo, hi = mapping[key]
        try:
            num = float(value)
        except ValueError:
            _reply(chat_id, "❌ Value must be a number.")
            return
        if not (lo <= num <= hi):
            _reply(chat_id, f"❌ Value must be between {lo:g} and {hi:g}.")
            return
        cfg = upsert_auto_trade_config(user["id"], wallet["id"], {col: num})
        label = {"amount": "Buy amount", "maxpos": "Max positions",
                 "tp": "Take profit", "sl": "Stop loss",
                 "cooldown": "Cooldown"}[key]
        shown = f"◎ {num:g}" if key == "amount" else f"{num:g}"
        _reply(chat_id, f"✅ {label} set to {shown}")
        return

    _reply(chat_id, "❌ Unknown subcommand. Try /auto, /auto on, /auto off, "
                    "/auto set &lt;key&gt; &lt;value&gt;")


# ── Subscription commands (Phase 4) ──────────────────────────────────

def _require_pro(chat_id: int) -> bool:
    """Gate for buy actions. Replies with the paywall if not Pro. Sells must
    never call this."""
    from bot.billing import subscription_status_line
    from bot.models import get_user_by_telegram_id, is_pro
    user = get_user_by_telegram_id(chat_id)
    if user and is_pro(user["id"]):
        return True
    _reply(chat_id,
        "🔒 <b>Pro feature</b>\n\n"
        f"{subscription_status_line(chat_id) if user else 'Send /start first'}\n\n"
        "💎 <b>0.2 SOL/week · 0.5 SOL/month</b>\n"
        "✅ Every call, real time · 🤖 24/7 auto-trading\n"
        "🎟 Promo codes drop daily on @thomas_young",
        reply_markup={"inline_keyboard": [
            [{"text": "💎 Pay 0.2 SOL — Week", "callback_data": "sub:sol_week"},
             {"text": "💎 Pay 0.5 SOL — Month", "callback_data": "sub:sol_month"}],
            [{"text": "⭐ Pay with Stars", "callback_data": "sub:stars_menu"}],
            [{"text": "🎟 I have a promo code", "callback_data": "promo:ask"}],
        ]})
    return False


def _subscribe_screen(chat_id: int):
    from bot import billing
    from bot.models import get_user_by_telegram_id
    user = get_user_by_telegram_id(chat_id)
    status = billing.subscription_status_line(user["id"]) if user else "Send /start first"
    _reply(chat_id,
        f"💎 <b>GEMBOT PRO</b>\n{status}\n\n"
        "⚡ Every call, real time\n"
        "🤖 Auto-trading 24/7 (your keys, your funds)\n"
        "🐋 Whale + insider alerts\n"
        "🏅 Milestone cards on every run\n\n"
        "<b>0.2 SOL / week &nbsp;·&nbsp; 0.5 SOL / month</b>",
        reply_markup={"inline_keyboard": [
            [{"text": "💎 Week — 0.2 SOL", "callback_data": "sub:sol_week"},
             {"text": "💎 Month — 0.5 SOL", "callback_data": "sub:sol_month"}],
            [{"text": "⭐ Pay with Telegram Stars", "callback_data": "sub:stars_menu"}],
            [{"text": "🎟 Promo code", "callback_data": "promo:ask"}],
        ]})


def _cmd_subscribe(chat_id: int, text: str):
    _subscribe_screen(chat_id)


def _cmd_promo(chat_id: int, text: str):
    from bot import billing
    parts = text.strip().split()
    if len(parts) < 2:
        _reply(chat_id,
            "🎟 <b>Promo code</b>\n\n"
            "Usage: <code>/promo GEM-XXXXXXXXXX</code>\n"
            "Codes drop daily on @thomas_young — first come, first served.\n"
            "⚠️ 3 attempts per day.",
            reply_markup={"inline_keyboard": [
                [{"text": "🎟 Enter code", "callback_data": "promo:ask"}]]})
        return
    if not billing.promo_allowed(chat_id):
        _reply(chat_id, "🛑 Too many attempts today (3 max). Try again tomorrow.")
        return
    ok, msg = billing.redeem(parts[1], chat_id)
    if ok:
        _reply(chat_id, f"🎉 <b>{msg}</b>\n\n/auto on to arm the bot.",
               reply_markup={"inline_keyboard": [
                   [{"text": "🤖 Arm Auto-Trading", "callback_data": "auto:menu"}]]})
    else:
        _reply(chat_id, f"❌ {html.escape(msg)}")


def _positions_screen(chat_id: int):
    """Open positions with live-ish PnL + sell buttons."""
    from bot.models import (
        get_user_by_telegram_id, get_open_positions, get_auto_trade_config,
        execute, close_cursor, _scalar,
    )
    from bot.tracker import _fetch_current_mcap
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first.")
        return
    c = execute("""
        SELECT p.* FROM positions p
        JOIN user_wallets w ON w.id = p.wallet_id
        WHERE p.user_id = %s AND p.status = 'open'
        ORDER BY p.created_at DESC LIMIT 10
    """, (user["id"],))
    positions = _dict_rows(c)
    close_cursor(c)
    if not positions:
        _reply(chat_id,
               "📭 <b>No open positions.</b>\n\n"
               "Arm /auto to catch the next call, or /buy manually.")
        return
    lines = ["📊 <b>Open positions</b>\n"]
    buttons = []
    for p in positions:
        entry = p.get("entry_mcap") or 0
        cur = _fetch_current_mcap(p["token_address"]) or 0
        pnl = ((cur / entry) - 1) * 100 if entry else 0
        sym = p.get("token_symbol") or p["token_address"][:6]
        icon = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{icon} <b>${html.escape(sym)}</b> — ◎{p.get('amount_sol_invested') or 0:.3f} in · {pnl:+.0f}%")
        buttons.append([
            {"text": f"Sell 50% ${sym}", "callback_data": f"sell:{p['token_address']}:50"},
            {"text": f"Sell 100%", "callback_data": f"sell:{p['token_address']}:100"},
        ])
    _reply(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buttons})


def _cmd_positions(chat_id: int, text: str):
    _positions_screen(chat_id)


# ── Callback router (inline buttons) ─────────────────────────────────

def _handle_callback(cb: dict):
    """Route inline-button presses. Always answers the callback."""
    from bot import billing
    from bot.models import get_user_by_telegram_id
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")

    def ack(text: str = None, alert: bool = False):
        if cb_id:
            payload = {"callback_query_id": cb_id}
            if text:
                payload["text"] = text
                payload["show_alert"] = alert
            billing._tg("answerCallbackQuery", payload)

    if chat_id is None:
        return

    if data == "gate:check":
        # bypass cache: fresh getChatMember for this check
        with billing._member_cache_lock:
            billing._member_cache.pop(chat_id, None)
        if billing.is_channel_member(chat_id):
            ack("✅ Verified!")
            _cmd_start(chat_id, "/start")
        else:
            ack("❌ Not detected yet — join, then tap again.", alert=True)
        return

    if data == "sub:menu":
        ack()
        _subscribe_screen(chat_id)
        return

    if data.startswith("sub:sol_"):
        plan = data.split("_", 1)[1]
        ack("⏳ Generating your deposit address...")
        inv = billing.create_invoice(get_user_id(chat_id), plan)
        if inv:
            _reply(chat_id,
                "💳 <b>SOL invoice</b>\n\n"
                f"Send <b>◎ {inv['amount']}</b> to:\n<code>{inv['address']}</code>\n\n"
                f"⏱ Expires in {inv['ttl_minutes']} min.\n"
                "Pro activates automatically after 1 confirmation.",
                reply_markup={"inline_keyboard": [[
                    {"text": "🔄 Check status", "callback_data": f"sub:check_{inv['invoice_id']}_{plan}"}]]})
        else:
            _reply(chat_id, "❌ Could not create invoice. Try again shortly.")
        return

    if data == "sub:stars_menu":
        ack()
        _reply(chat_id, "⭐ <b>Pay with Telegram Stars</b>",
               reply_markup={"inline_keyboard": [
                   [{"text": f"⭐ Week — {billing.STARS_PLANS['week']['stars']} stars",
                     "callback_data": "sub:stars_week"}],
                   [{"text": f"⭐ Month — {billing.STARS_PLANS['month']['stars']} stars",
                     "callback_data": "sub:stars_month"}]]})
        return

    if data.startswith("sub:stars_"):
        plan = data.rsplit("_", 1)[1]
        ack("🧾 Opening payment...")
        billing.send_stars_invoice(chat_id, plan)
        return

    if data == "promo:ask":
        ack()
        _reply(chat_id,
               "🎟 <b>Enter your code</b>\n\n"
               "Send: <code>/promo GEM-XXXXXXXXXX</code>\n"
               "Codes drop daily on @thomas_young")
        return

    if data == "auto:menu":
        ack()
        _cmd_auto(chat_id, "/auto")
        return

    if data.startswith("auto:amt_"):
        amount = float(data.rsplit("_", 1)[1])
        uid = get_user_id(chat_id)
        if not uid:
            ack("Send /start first.", alert=True)
            return
        from bot.models import get_default_wallet, upsert_auto_trade_config
        wallet = get_default_wallet(uid)
        if not wallet:
            ack("No wallet — send /start.", alert=True)
            return
        cfg = upsert_auto_trade_config(uid, wallet["id"],
                                       {"buy_amount_sol": amount})
        ack(f"Buy amount set to ◎ {amount:g}")
        _reply(chat_id,
               f"✅ <b>Auto-buy amount: ◎ {amount:g} per call</b>\n"
               f"TP +{cfg['take_profit_pct']:.0f}% / SL -{cfg['stop_loss_pct']:.0f}%\n"
               "🧼 Wash gate still applies: auto-buy only fires on clean calls.",
               reply_markup=_auto_menu_markup(cfg))
        return

    if data.startswith("auto:tp_"):
        pct = float(data.rsplit("_", 1)[1])
        uid = get_user_id(chat_id)
        wallet = get_default_wallet(uid) if uid else None
        if not wallet:
            ack("Send /start first.", alert=True)
            return
        cfg = upsert_auto_trade_config(uid, wallet["id"],
                                       {"take_profit_pct": pct})
        ack(f"Take profit set to +{pct:g}%")
        _reply(chat_id, f"✅ <b>Take profit: +{pct:g}%</b>",
               reply_markup=_auto_menu_markup(cfg))
        return

    if data.startswith("auto:sl_"):
        pct = float(data.rsplit("_", 1)[1])
        uid = get_user_id(chat_id)
        wallet = get_default_wallet(uid) if uid else None
        if not wallet:
            ack("Send /start first.", alert=True)
            return
        cfg = upsert_auto_trade_config(uid, wallet["id"],
                                       {"stop_loss_pct": pct})
        ack(f"Stop loss set to -{pct:g}%")
        _reply(chat_id, f"✅ <b>Stop loss: -{pct:g}%</b>",
               reply_markup=_auto_menu_markup(cfg))
        return

    if data == "auto:toggle":
        uid = get_user_id(chat_id)
        wallet = get_default_wallet(uid) if uid else None
        if not wallet:
            ack("Send /start first.", alert=True)
            return
        from bot.models import get_auto_trade_config
        cfg = get_auto_trade_config(uid, wallet["id"])
        currently_on = bool(cfg and cfg["is_enabled"])
        if currently_on:
            _cmd_auto(chat_id, "/auto off")
            ack("Auto-trading OFF")
        else:
            _cmd_auto(chat_id, "/auto on")
            ack("Auto-trading ON")
        return

    if data == "pos:list":
        ack()
        _positions_screen(chat_id)
        return

    if data.startswith("sell:"):
        _, addr, pct = data.split(":")
        ack("⏳ Selling...")
        _cmd_sell(chat_id, f"/sell {addr} {pct}")
        return

    ack()


def get_user_id(chat_id):
    """user_id (DB) for a telegram chat_id, or None."""
    from bot.models import get_user_by_telegram_id
    u = get_user_by_telegram_id(chat_id)
    return u["id"] if u else None


# ── Command router ────────────────────────────────────────────────────

_COMMANDS = {
    "/start": _cmd_start,
    "/wallet": _cmd_wallet,
    "/balance": _cmd_balance,
    "/buy": _cmd_buy,
    "/sell": _cmd_sell,
    "/slippage": _cmd_slippage,
    "/status": _cmd_status,
    "/auto": _cmd_auto,
    "/subscribe": _cmd_subscribe,
    "/promo": _cmd_promo,
    "/positions": _cmd_positions,
}


def _route(chat_id: int, text: str):
    """Route a message to the right command handler."""
    if not text.startswith("/"):
        return
    cmd = text.split()[0].lower()
    handler = _COMMANDS.get(cmd)
    if handler:
        handler(chat_id, text)
    else:
        _reply(chat_id,
            "❌ Unknown command.\n\n"
            "Available: /start, /wallet, /balance, /buy, /sell, /slippage, "
            "/status, /auto, /positions, /subscribe, /promo"
        )


# ── Listener loop ─────────────────────────────────────────────────────

def _load_offset() -> int:
    try:
        with open(_OFFSET_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_offset(offset: int):
    try:
        with open(_OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except OSError:
        pass


def listen():
    """Poll Telegram: commands, button presses, Stars payments."""
    from bot import billing
    _last_update_id = _load_offset()

    while True:
        try:
            url = f"{_API}/getUpdates"
            params = {
                "offset": _last_update_id + 1,
                "timeout": 3,  # short: pre_checkout must be answered <10s
                "allowed_updates": ["message", "callback_query",
                                    "pre_checkout_query"],
            }
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                continue

            updates = data.get("result", [])
            # Answer payment checks FIRST — they die after 10s
            updates.sort(key=lambda u: 0 if "pre_checkout_query" in u else 1)

            for update in updates:
                _last_update_id = update["update_id"]
                _save_offset(_last_update_id)

                # ── Stars: pre-checkout (answer fast!) ───────────────
                pq = update.get("pre_checkout_query")
                if pq:
                    try:
                        billing.handle_pre_checkout_query(pq)
                    except Exception:
                        logger.exception("pre_checkout failed")
                        billing._tg("answerPreCheckoutQuery", {
                            "pre_checkout_query_id": pq.get("id"), "ok": False})
                    continue

                # ── Inline buttons ───────────────────────────────────
                cb = update.get("callback_query")
                if cb:
                    try:
                        _handle_callback(cb)
                    except Exception:
                        logger.exception("callback failed")
                    continue

                msg = update.get("message", {})
                chat = msg.get("chat", {})
                chat_id = chat.get("id")

                # ── Stars: successful payment ────────────────────────
                if msg.get("successful_payment") and chat_id:
                    try:
                        billing.handle_successful_payment(chat_id, msg)
                    except Exception:
                        logger.exception("payment handling failed")
                    continue

                text = msg.get("text", "")
                if not chat_id:
                    continue

                # Rate limit normal commands (payments already handled above)
                if not _check_rate_limit(chat_id):
                    continue

                if text.strip().startswith("/"):
                    _route(chat_id, text.strip())

        except requests.Timeout:
            pass
        except requests.RequestException as e:
            logger.debug("Listener poll error: %s", e)
            time.sleep(5)

        time.sleep(_POLL_INTERVAL)


def start_listener():
    """Start the listener in a background daemon thread."""
    thread = threading.Thread(target=listen, daemon=True, name="tg-listener")
    thread.start()
    logger.info("Telegram listener thread started")
    return thread