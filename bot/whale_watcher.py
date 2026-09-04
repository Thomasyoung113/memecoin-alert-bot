"""
Whale Watcher — monitors known whale wallets for new token buys.
Uses Helius/Solana RPC to check recent transactions every 5 minutes.
Alerts via Telegram when a whale buys a token not yet seen.
"""
import logging
import time
import threading

from config import WHALE_WATCH_LIST, WHALE_WATCH_INTERVAL
from bot.telegram import send_update

logger = logging.getLogger(__name__)

# Track token mints we've already alerted on to avoid duplicates
_seen_tokens: set = set()


def _load_seen():
    """Load previously seen token addresses from DB to survive restarts."""
    from bot.models import execute, close_cursor
    c = execute("SELECT DISTINCT token_address FROM whale_alerts")
    rows = c.fetchall()
    close_cursor(c)
    for r in rows:
        _seen_tokens.add(r[0])
    logger.debug("Loaded %d previously seen whale tokens", len(_seen_tokens))


def _save_alert(token_address: str, whale_address: str):
    """Persist a whale alert to the DB (deduplicated by token+whale)."""
    from bot.models import execute, commit, close_cursor
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c = execute("""
        INSERT INTO whale_alerts
            (token_address, whale_address, detected_at)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (token_address, whale_address, now))
    close_cursor(c)
    commit()


def _get_token_symbol(token_address: str) -> str:
    """Fetch a human-readable symbol from DexScreener, or fall back to short address."""
    from bot.wide_scanner import _fetch_token_profile_by_address
    pair = _fetch_token_profile_by_address(token_address)
    if pair and pair.get("symbol"):
        return pair["symbol"]
    return token_address[:8]


def _get_recent_buys(wallet_address: str, limit: int = 20) -> list[str]:
    """
    Return token mint addresses the wallet appears to have bought
    in its most recent transactions.

    Uses postTokenBalances minus preTokenBalances per tx to detect
    newly acquired tokens.
    """
    from bot.helius import _rpc_call, get_transaction

    result = _rpc_call("getSignaturesForAddress", [
        wallet_address,
        {"limit": limit, "commitment": "confirmed"},
    ])
    if not result or "result" not in result:
        return []

    sigs = [s["signature"] for s in result["result"]]
    bought = []

    for sig in sigs:
        tx = get_transaction(sig)
        if not tx:
            continue

        meta = tx.get("meta")
        if not meta:
            continue

        post_balances = meta.get("postTokenBalances", [])
        pre_balances = meta.get("preTokenBalances", [])

        # Tokens the wallet already held before this tx
        pre_tokens = {b["mint"] for b in pre_balances if b.get("owner") == wallet_address}

        for bal in post_balances:
            if bal.get("owner") != wallet_address:
                continue
            mint = bal.get("mint")
            if mint and mint not in pre_tokens:
                bought.append(mint)

    return bought


def check_whale_wallet(whale_address: str) -> list[str]:
    """
    Check transactions for one whale wallet.
    Returns token address(es) not yet seen.
    """
    tokens = _get_recent_buys(whale_address)
    new_tokens = []
    for t in tokens:
        if t not in _seen_tokens:
            _seen_tokens.add(t)
            new_tokens.append(t)
    return new_tokens


def whale_watch_cycle() -> int:
    """
    One full watch cycle across all configured whale wallets.
    Returns count of new buys alerted.
    """
    if not _seen_tokens:
        _load_seen()

    if not WHALE_WATCH_LIST:
        return 0

    total = 0
    for whale in WHALE_WATCH_LIST:
        try:
            new_tokens = check_whale_wallet(whale)
            if not new_tokens:
                continue

            whale_short = f"{whale[:4]}...{whale[-4:]}"
            for token_addr in new_tokens:
                symbol = _get_token_symbol(token_addr)
                token_short = f"{token_addr[:6]}...{token_addr[-4:]}"
                chart_url = f"https://dexscreener.com/solana/{token_addr}"

                msg = (
                    f"🐋 <b>Whale Buy Detected!</b>\n\n"
                    f"👛 Wallet: <code>{whale_short}</code>\n"
                    f"🪙 Token: <b>${symbol}</b> (<code>{token_short}</code>)\n"
                    f"\n"
                    f"🔗 <a href='{chart_url}'>View on DexScreener</a>"
                )
                send_update(msg)

                _save_alert(token_addr, whale)
                logger.info("🐋 Whale %s bought %s (%s)",
                            whale_short, symbol, token_addr[:8])
                total += 1

        except Exception as e:
            logger.exception("Error checking whale wallet %s: %s",
                             whale[:8], e)

    return total


def start_whale_watcher():
    """
    Start the whale watcher in a background daemon thread.
    Runs every WHALE_WATCH_INTERVAL seconds.

    Follows the same pattern as wide_scanner.start_wide_scanner().
    """
    # Ensure the whale_alerts table exists
    from bot.models import execute, commit, close_cursor
    c = execute("""
        CREATE TABLE IF NOT EXISTS whale_alerts (
            id              SERIAL PRIMARY KEY,
            token_address   TEXT NOT NULL,
            whale_address   TEXT NOT NULL,
            detected_at     TIMESTAMP,
            UNIQUE(token_address, whale_address)
        )
    """)
    close_cursor(c)
    commit()

    def _loop():
        logger.info("Whale watcher started (interval: %ds, wallets: %d)",
                     WHALE_WATCH_INTERVAL, len(WHALE_WATCH_LIST))
        while True:
            try:
                new = whale_watch_cycle()
                if new:
                    logger.info("Whale watcher: %d new buy(s)", new)
            except Exception as e:
                logger.exception("Whale watcher error: %s", e)
            time.sleep(WHALE_WATCH_INTERVAL)

    thread = threading.Thread(target=_loop, daemon=True, name="whale-watcher")
    thread.start()
    logger.info("Whale watcher thread started")
    return thread