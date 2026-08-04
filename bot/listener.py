"""
Telegram listener — polls for user commands and dispatches them.
Supports multi-user wallet management (Phase 1).

Commands:
  /start              — Create profile + auto-generate first wallet
  /wallet             — Show current default wallet info
  /wallet new <label> — Generate a new wallet with a label
  /wallet list        — List all wallets
  /wallet default <label> — Set default wallet
  /wallet export      — ⚠️ Export private key (use with caution)
  /balance            — Show SOL + token balances

Runs in a background daemon thread alongside the main scanner.
"""
import html
import logging
import os
import time
import threading
import re

import requests

from config import TELEGRAM_BOT_TOKEN
from bot.models import (
    get_or_create_user, get_user_by_telegram_id,
    get_user_wallets, get_default_wallet, create_user_wallet,
    set_default_wallet, get_wallet_by_label,
    save_trade, get_user_trades, get_trade_by_sig, update_trade_status,
)
from bot.wallet import generate_wallet, get_wallet_info, decrypt_private_key
from bot.jupiter import buy_token, sell_token, get_quote, check_transaction_status, get_token_decimals
from bot.helius import SOLANA_RPC

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

def _reply(chat_id: int, text: str, parse_mode: str = "HTML"):
    """Send a reply message to a chat."""
    try:
        requests.post(f"{_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }, timeout=10)
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
    """Create user profile + first wallet."""
    # Get the sender's telegram_id from the message context isn't available here
    # We use chat_id as telegram_id (works for DMs)
    user = get_or_create_user(chat_id)
    wallets = get_user_wallets(user["id"])
    if not wallets:
        try:
            pubkey, encrypted, _ = generate_wallet()
            wallet = create_user_wallet(user["id"], "main", pubkey, encrypted)
            _reply(chat_id,
                "🚀 <b>Welcome to GEMBOT!</b>\n\n"
                "✅ Your Solana wallet has been created:\n"
                f"<code>{pubkey}</code>\n\n"
                "📋 <b>Available commands:</b>\n"
                "  /wallet — View wallet info\n"
                "  /wallet new &lt;label&gt; — Create another wallet\n"
                "  /wallet list — List all wallets\n"
                "  /wallet default &lt;label&gt; — Set default\n"
                "  /wallet export — ⚠️ Export private key\n"
                "  /balance — Check SOL + token balances\n\n"
                "💡 Fund your wallet with SOL to start trading.\n"
                "Stay ahead. 🐋"
            )
        except RuntimeError as e:
            _reply(chat_id, f"❌ {e}")
    else:
        default = get_default_wallet(user["id"])
        addr = _short_pubkey(default["public_key"]) if default else "?"
        _reply(chat_id,
            "👋 <b>Welcome back!</b>\n\n"
            f"Default wallet: {addr}\n\n"
            "Use /wallet to see details or /balance for balances."
        )


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
    """Buy tokens: /buy <token_address> <amount_sol>"""
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

    # Get quote first for preview
    from bot.jupiter import SOL_MINT, LAMPORTS_PER_SOL
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    quote = get_quote(SOL_MINT, token_address, amount_lamports)
    if not quote:
        _reply(chat_id, "❌ Could not get a quote for this token. Check the address.")
        return

    out_amount = int(quote.get("outAmount", 0))
    price_impact = float(quote.get("priceImpactPct", 0))
    routes = len(quote.get("routePlan", []))

    _reply(chat_id,
        f"📊 <b>Buy Preview</b>\n\n"
        f"Spend: ◎ {amount_sol:.4f} SOL\n"
        f"Expected: {out_amount:,} tokens\n"
        f"Price impact: {price_impact:.2f}%\n"
        f"Routes: {routes}\n"
        f"Slippage: {wallet.get('slippage_bps', 500) / 100:.1f}%\n\n"
        f"To confirm, send:\n"
        f"<code>/buy confirm {token_address[:8]}...</code>"
    )


def _cmd_buy_confirm(chat_id: int, text: str):
    """Confirm and execute buy: /buy confirm <partial_token_address>"""
    user = get_user_by_telegram_id(chat_id)
    if not user:
        _reply(chat_id, "❌ Send /start first.")
        return
    parts = text.strip().split()
    if len(parts) < 3:
        _reply(chat_id, "❌ Usage: /buy confirm &lt;token_address_prefix&gt;")
        return

    wallet = get_default_wallet(user["id"], include_privkey=True)
    if not wallet:
        _reply(chat_id, "❌ No wallet.")
        return

    # Get the last buy command context from user_data isn't available in polling mode
    # So we execute with what we have - the address prefix
    _reply(chat_id, "⏳ Executing swap... This may take a few seconds.")
    try:
        raw_key = decrypt_private_key(wallet["encrypted_private_key"])
        sig = buy_token(
            token_address=parts[2],  # will need full address
            amount_sol=0.01,  # placeholder - need to store amount
            user_pubkey=wallet["public_key"],
            keypair_bytes=raw_key,
        )
        if sig:
            # Look up token symbol
            from bot.models import execute, commit, close_cursor, _dict_rows
            c = execute("SELECT token_symbol FROM trades WHERE user_id = %s ORDER BY id DESC LIMIT 1",
                        (user["id"],))
            row = c.fetchone()
            symbol = row[0] if row else parts[2][:8]
            close_cursor(c)

            _reply(chat_id,
                f"✅ <b>Buy Executed!</b>\n\n"
                f"Token: ${symbol}\n"
                f"Tx: <code>{sig[:16]}...</code>\n\n"
                f"🔗 <a href='https://solscan.io/tx/{sig}'>View on Solscan</a>"
            )
        else:
            _reply(chat_id, "❌ Swap failed. Check token address and try again.")
    except Exception as e:
        logger.error("Buy failed for user %s: %s", chat_id, e)
        _reply(chat_id, "❌ Swap failed. Please try again later.")


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

    try:
        raw_key = decrypt_private_key(wallet["encrypted_private_key"])
        sig = sell_token(
            token_address=token_address,
            amount_tokens=amount,
            user_pubkey=wallet["public_key"],
            keypair_bytes=raw_key,
        )
        if sig:
            _reply(chat_id,
                f"✅ <b>Sell Executed!</b>\n\n"
                f"Sold: {pct}%\n"
                f"Tx: <code>{sig[:16]}...</code>\n\n"
                f"🔗 <a href='https://solscan.io/tx/{sig}'>View on Solscan</a>"
            )
        else:
            _reply(chat_id, "❌ Sell failed.")
    except Exception as e:
        logger.error("Sell failed for user %s: %s", chat_id, e)
        _reply(chat_id, "❌ Sell failed.")


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


# ── Command router ────────────────────────────────────────────────────

_COMMANDS = {
    "/start": _cmd_start,
    "/wallet": _cmd_wallet,
    "/balance": _cmd_balance,
    "/buy": _cmd_buy_confirm,
    "/sell": _cmd_sell,
    "/slippage": _cmd_slippage,
    "/status": _cmd_status,
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
            "Available: /start, /wallet, /wallet new &lt;label&gt;, /wallet list, /wallet default &lt;label&gt;, /wallet export confirm, /balance, /buy, /sell, /slippage, /status"
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
    """Poll Telegram for messages and route commands."""
    _last_update_id = _load_offset()

    while True:
        try:
            url = f"{_API}/getUpdates"
            params = {
                "offset": _last_update_id + 1,
                "timeout": 10,
                "allowed_updates": ["message"],
            }
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                continue

            for update in data.get("result", []):
                _last_update_id = update["update_id"]
                _save_offset(_last_update_id)
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = msg.get("text", "")

                if not chat_id:
                    continue

                # Rate limit check
                if not _check_rate_limit(chat_id):
                    continue

                # Route command
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