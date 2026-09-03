"""
auto_trader.py — Phase 3: automatic trading engine (instant buy / instant sell).

When a user enables auto-trading (/auto on), the bot buys every token it
alerts with that user's wallet via Jupiter, then manages the position:

  ── Buy trigger ──────────────────────────────────────────────────────
  maybe_auto_buy(...)  — called right after an alert is broadcast.
  Fires an instant Jupiter buy for every enabled auto-trade config,
  behind guardrails: already-held token, max open positions, cooldown
  between buys, and a SOL balance check.

  ── Sell triggers ────────────────────────────────────────────────────
  maybe_auto_sell(r)    — called when an alert resolves (2x win / -50%
                          loss / stale): closes every open position on
                          that token.
  check_all_positions() — called every outcome-check cycle: sweeps all
                          open positions against each config's take-profit
                          / stop-loss thresholds (vs entry mcap) and sells
                          instantly when a threshold is crossed.

Every fill is recorded in the positions table (for PnL tracking) and the
trades table (for history / sell-card entry price lookup).

Usage (wired in main.py):
    from bot.auto_trader import maybe_auto_buy, maybe_auto_sell, check_all_positions
"""
import logging
import time
from datetime import datetime, timezone

from bot.models import (
    get_enabled_auto_traders, get_auto_trade_config, get_wallet_by_id,
    count_open_positions, get_last_position_time, has_any_position,
    create_position, get_open_positions, close_position, save_trade,
)
from bot.wallet import decrypt_private_key, get_sol_balance, get_token_balances
from bot.jupiter import (
    get_quote, get_swap_transaction, submit_transaction,
    get_token_decimals, SOL_MINT, LAMPORTS_PER_SOL,
)
from bot.tracker import _fetch_current_mcap

logger = logging.getLogger(__name__)

# Guardrails
MIN_SOL_BUFFER = 0.02        # keep this much SOL for fees + account rent
MIN_BUY_SOL = 0.001          # Jupiter dust floor
MAX_BUY_SOL = 10.0           # hard cap per buy regardless of config
DEFAULT_TP_PCT = 100.0       # +100% vs entry mcap
DEFAULT_SL_PCT = 50.0        # -50% vs entry mcap
DEFAULT_COOLDOWN_MIN = 0     # no cooldown unless configured


# ── Small helpers ─────────────────────────────────────────────────────

def _ts(value) -> float | None:
    """DB timestamp (datetime or ISO string) → unix seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _clamp_buy_amount(amount) -> float:
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0.0
    return max(MIN_BUY_SOL, min(MAX_BUY_SOL, amt))


def _tp_sl(config) -> tuple[float, float]:
    """(take_profit_pct, stop_loss_pct) for a config, with safe defaults."""
    tp = float(config.get("take_profit_pct") or DEFAULT_TP_PCT)
    sl = float(config.get("stop_loss_pct") or DEFAULT_SL_PCT)
    return tp, sl


def _pnl_pct(entry_mcap, current_mcap) -> float | None:
    try:
        if entry_mcap and current_mcap and entry_mcap > 0:
            return (current_mcap / entry_mcap - 1) * 100
    except (TypeError, ZeroDivisionError):
        pass
    return None


# ── BUY side ──────────────────────────────────────────────────────────

def maybe_auto_buy(token_address: str, symbol: str, mcap: float,
                   price: float, snapshot: dict) -> list[dict]:
    """
    Instant-buy an alerted token for every enabled auto-trade config.

    Returns a list of dicts:
      {"symbol", "amount_sol", "tx_sig", "user_id", "wallet_id", "position_id"}
    """
    from bot.billing import is_pro
    configs = get_enabled_auto_traders()
    if not configs:
        return []

    results = []
    for cfg in configs:
        try:
            # Subscription gate: only trial/Pro users auto-trade.
            # (Sells are NEVER gated — check_all_positions runs for everyone.)
            if not is_pro(cfg["user_id"]):
                logger.debug("Auto-buy gated (not Pro): user %s", cfg["user_id"])
                continue
            trade = _auto_buy_for_config(cfg, token_address, symbol, mcap)
            if trade:
                results.append(trade)
        except Exception as e:
            logger.exception("Auto-buy failed for wallet %s on %s: %s",
                             cfg.get("wallet_id"), symbol, e)
    return results


def _auto_buy_for_config(cfg: dict, token_address: str,
                         symbol: str, mcap: float) -> dict | None:
    user_id, wallet_id = cfg["user_id"], cfg["wallet_id"]
    amount_sol = _clamp_buy_amount(cfg.get("buy_amount_sol"))

    # Already traded this token with this wallet? Never double-buy.
    if has_any_position(user_id, wallet_id, token_address):
        return None

    # Max open positions per wallet
    max_positions = int(cfg.get("max_positions") or 3)
    if count_open_positions(user_id, wallet_id) >= max_positions:
        logger.debug("Auto-buy skipped (max positions %d): %s",
                     max_positions, symbol)
        return None

    # Cooldown since the wallet's last position
    last = _ts(get_last_position_time(user_id, wallet_id))
    cooldown_s = float(cfg.get("cooldown_minutes") or DEFAULT_COOLDOWN_MIN) * 60
    if last is not None and cooldown_s > 0 and time.time() - last < cooldown_s:
        logger.debug("Auto-buy skipped (cooldown): %s", symbol)
        return None

    wallet = get_wallet_by_id(wallet_id, include_privkey=True)
    if not wallet:
        logger.warning("Auto-buy config %s has no wallet", cfg.get("id"))
        return None

    # Balance check (leave room for fees + ATA rent)
    balance = get_sol_balance(wallet["public_key"])
    if balance < amount_sol + MIN_SOL_BUFFER:
        logger.info("Auto-buy skipped (balance ◎%.4f < ◎%.4f): %s",
                    balance, amount_sol + MIN_SOL_BUFFER, symbol)
        return None

    slippage_bps = int(wallet.get("slippage_bps") or 500)

    # Quote first — also validates the token is tradeable
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    quote = get_quote(SOL_MINT, token_address, amount_lamports, slippage_bps)
    if not quote:
        logger.info("Auto-buy skipped (no quote): %s", symbol)
        return None

    # Instant buy: build swap tx and submit
    base64_tx = get_swap_transaction(quote, wallet["public_key"])
    if not base64_tx:
        logger.warning("Auto-buy swap-tx failed: %s", symbol)
        return None
    raw_key = decrypt_private_key(wallet["encrypted_private_key"])
    sig = submit_transaction(base64_tx, raw_key)
    if not sig:
        logger.warning("Auto-buy submit failed: %s (wallet %s)",
                       symbol, wallet["label"])
        return None

    # Record the position + trade
    out_raw = int(quote.get("outAmount", 0))
    in_lamports = int(quote.get("inAmount", 0)) or amount_lamports
    decimals = get_token_decimals(token_address)
    amount_token = out_raw / (10 ** decimals) if out_raw else None
    entry_price_sol = None
    if out_raw:
        entry_price_sol = (in_lamports / LAMPORTS_PER_SOL) / (out_raw / (10 ** decimals))
    position_id = create_position(
        user_id, wallet_id, None, token_address, symbol,
        entry_mcap=mcap, entry_price_sol=entry_price_sol,
        amount_sol_invested=amount_sol, amount_token=amount_token,
        token_decimals=decimals,
    )
    save_trade(user_id, wallet_id, "buy", token_address, token_symbol=symbol,
               amount_sol=amount_sol, amount_token=amount_token,
               price_sol=entry_price_sol, tx_signature=sig,
               slippage_bps=slippage_bps)

    logger.info("🤖 Auto-bought %s: ◎%.4f (wallet %s, tx %s...)",
                symbol, amount_sol, wallet["label"], sig[:12])
    return {
        "symbol": symbol,
        "amount_sol": amount_sol,
        "tx_sig": sig,
        "user_id": user_id,
        "wallet_id": wallet_id,
        "position_id": position_id,
    }


# ── SELL side ─────────────────────────────────────────────────────────

def maybe_auto_sell(resolution: dict) -> list[dict]:
    """
    Close every open position on a token when its alert resolves.

    Expects the resolution dict from tracker.check_outcomes():
      {"symbol", "token_address", "hit_2x", "hit_loss", "current_mcap", ...}

    Returns sell results, same shape as check_all_positions().
    """
    token_address = resolution.get("token_address")
    if not token_address:
        return []

    if resolution.get("hit_2x"):
        reason = "Take profit at 2x"
    elif resolution.get("hit_loss"):
        reason = "Stop loss at -50%"
    else:
        reason = "Alert window ended"

    return _close_all_for_token(token_address, reason,
                                current_mcap=resolution.get("current_mcap"))


def check_all_positions() -> list[dict]:
    """
    Sweep all open positions against each config's TP/SL thresholds and
    instantly sell anything that crossed its line. Called every outcome
    check cycle so exits fire between alert resolutions too.

    Returns a list of sell results:
      {"symbol", "token_address", "tx_sig", "pnl_pct", "reason",
       "user_id", "wallet_id"}
    """
    positions = get_open_positions()
    if not positions:
        return []

    results = []
    mcaps: dict[str, float | None] = {}
    for pos in positions:
        entry_mcap = pos.get("entry_mcap")
        if not entry_mcap:
            continue

        addr = pos["token_address"]
        if addr not in mcaps:
            mcaps[addr] = _fetch_current_mcap(addr)
        current_mcap = mcaps[addr]
        if not current_mcap:
            continue

        gain_pct = _pnl_pct(entry_mcap, current_mcap)
        if gain_pct is None:
            continue

        config = get_auto_trade_config(pos["user_id"], pos["wallet_id"])
        tp_pct, sl_pct = _tp_sl(config or {})

        if gain_pct >= tp_pct:
            reason = f"Take profit +{gain_pct:.0f}%"
        elif gain_pct <= -sl_pct:
            reason = f"Stop loss {gain_pct:.0f}%"
        else:
            continue

        trade = _execute_sell(pos, reason, current_mcap)
        if trade:
            results.append(trade)
    return results


def _close_all_for_token(token_address: str, reason: str,
                         current_mcap: float | None = None) -> list[dict]:
    results = []
    for pos in get_open_positions(token_address):
        trade = _execute_sell(pos, reason, current_mcap)
        if trade:
            results.append(trade)
    return results


def _execute_sell(pos: dict, reason: str,
                  current_mcap: float | None = None) -> dict | None:
    """Sell 100% of a position's token balance and close it out."""
    wallet = get_wallet_by_id(pos["wallet_id"], include_privkey=True)
    if not wallet:
        close_position(pos["id"], _pnl_pct(pos.get("entry_mcap"), current_mcap),
                       "wallet missing")
        return None

    # What does the wallet actually hold? (source of truth on-chain)
    balance = None
    for t in get_token_balances(wallet["public_key"]):
        if t["mint"] == pos["token_address"]:
            balance = t
            break
    if not balance or balance["amount"] <= 0:
        logger.info("Auto-sell skipped (no balance): position %s", pos["id"])
        close_position(pos["id"], _pnl_pct(pos.get("entry_mcap"), current_mcap),
                       "no balance")
        return None

    decimals = balance.get("decimals") or pos.get("token_decimals") or 6
    amount_raw = int(balance["amount"] * (10 ** decimals))

    if current_mcap is None:
        current_mcap = _fetch_current_mcap(pos["token_address"])
    pnl = _pnl_pct(pos.get("entry_mcap"), current_mcap)

    slippage_bps = int(wallet.get("slippage_bps") or 500)
    try:
        raw_key = decrypt_private_key(wallet["encrypted_private_key"])
        sig = sell_token_side(pos["token_address"], amount_raw,
                              wallet["public_key"], raw_key, slippage_bps)
    except Exception as e:
        logger.error("Auto-sell failed for position %s: %s", pos["id"], e)
        return None
    if not sig:
        logger.warning("Auto-sell submit failed, will retry next cycle: pos %s",
                       pos["id"])
        return None

    close_position(pos["id"], pnl, reason)
    save_trade(pos["user_id"], pos["wallet_id"], "sell", pos["token_address"],
               token_symbol=pos.get("token_symbol"),
               amount_token=balance["amount"], tx_signature=sig,
               slippage_bps=slippage_bps)

    logger.info("🤖 Auto-sold %s (pos %s): %s, pnl %.1f%%",
                pos.get("token_symbol"), pos["id"], reason, pnl or 0.0)

    # ── Card enrichment: multiplier, SOL figures, hold time ──────────
    sol_received = _fetch_sol_received(sig)
    if sol_received is None and pos.get("amount_sol_invested") and pnl is not None:
        # fallback estimate from entry economics
        sol_received = pos["amount_sol_invested"] * (1 + pnl / 100.0)
    multiplier = None
    if pos.get("entry_mcap") and current_mcap:
        multiplier = current_mcap / pos["entry_mcap"]

    return {
        "symbol": pos.get("token_symbol") or pos["token_address"][:8],
        "token_address": pos["token_address"],
        "tx_sig": sig,
        "pnl_pct": pnl,
        "reason": reason,
        "user_id": pos["user_id"],
        "wallet_id": pos["wallet_id"],
        "amount_token": balance["amount"],
        "current_mcap": current_mcap,
        "entry_mcap": pos.get("entry_mcap"),
        "duration": _held_duration(pos),
        "multiplier": multiplier,
        "sol_spent": pos.get("amount_sol_invested"),
        "sol_received": sol_received,
        "wallet_label": wallet.get("label"),
    }


def _fetch_sol_received(tx_sig: str) -> float | None:
    """Actual SOL out of a sell tx, parsed from its on-chain transaction."""
    from bot.wallet import _rpc_call
    result = _rpc_call("getTransaction", [
        tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])
    try:
        meta = result["result"]["meta"]
        if meta.get("err"):
            return None
        owner = (result["result"]["transaction"]["message"]["accountKeys"][0]
                 ["pubkey"])
        for post, pre in zip(meta["postTokenBalances"], meta["preTokenBalances"]):
            if (post.get("owner") == owner
                    and post.get("mint") == SOL_MINT
                    and post.get("uiAmount") is not None):
                return float(post["uiAmount"]) - float(pre.get("uiAmount") or 0)
    except (TypeError, KeyError, IndexError):
        pass
    return None


def _held_duration(pos: dict) -> str:
    created = _ts(pos.get("created_at"))
    if created is None:
        return None
    seconds = max(0, time.time() - created)
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


def sell_token_side(token_address: str, amount_tokens: int,
                    user_pubkey: str, keypair_bytes: bytes,
                    slippage_bps: int = 500) -> str | None:
    """Jupiter sell — thin wrapper so _execute_sell stays readable."""
    from bot.jupiter import sell_token
    return sell_token(
        token_address=token_address,
        amount_tokens=amount_tokens,
        user_pubkey=user_pubkey,
        keypair_bytes=keypair_bytes,
        slippage_bps=slippage_bps,
    )
