"""
Telegram listener — polls for /start and replies with welcome message.
Runs in a separate daemon thread alongside the main scanner.
"""
import logging
import os
import time
import threading

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_POLL_INTERVAL = 3
_OFFSET_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "tg_offset.txt")
_OWNER_CHAT_ID = TELEGRAM_CHAT_ID


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


def _send_welcome(chat_id: int):
    """Send a welcome message to a user who started the bot."""
    text = (
        "🚀 <b>Gem Alert Bot</b>\n\n"
        "I scan Solana memecoins on DexScreener, verify them with RugCheck, "
        "and alert you when a token shows <b>2x potential</b>.\n\n"
        "📊 <b>What I do:</b>\n"
        "• Scan new tokens every 30s\n"
        "• Filter by MCap, volume, age, buys/sells\n"
        "• Check LP lock & safety via RugCheck\n"
        "• Track outcomes — learn & improve filters\n"
        "• Monitor smart money wallets for alpha\n\n"
        "🔔 <b>You'll get alerts automatically</b> when a gem is spotted.\n"
        "No commands needed — just sit back and watch.\n\n"
        "Built by @thomas_young"
    )
    url = f"{_API}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        logger.info("Welcome sent to chat %d", chat_id)
    except requests.RequestException as e:
        logger.warning("Failed to send welcome: %s", e)


def listen():
    """Poll Telegram for /start messages (runs in a thread)."""
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
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")

                if not chat_id:
                    continue

                # Whitelist: only respond to the owner
                if str(chat_id) != str(_OWNER_CHAT_ID):
                    logger.info("Ignored message from unknown chat %d", chat_id)
                    continue

                # Handle /start command
                if text.strip() == "/start":
                    _send_welcome(chat_id)

        except requests.Timeout:
            # Long-poll timeout is expected — just loop
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