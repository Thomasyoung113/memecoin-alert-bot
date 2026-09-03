"""
jupiter.py — Jupiter swap integration for instant buy/sell on Solana.

Uses Jupiter v6 API for quotes + swap tx building.
Signs and submits transactions via Solana RPC (public or Helius).

Flow:
  1. Get quote: GET https://quote-api.jup.ag/v6/quote?...
  2. Get swap tx: POST https://quote-api.jup.ag/v6/swap
  3. Sign + submit: Deserialize tx, sign with solders, send to RPC

Usage:
    from bot.jupiter import buy_token, sell_token

    sig = buy_token(
        token_address="So111...",
        amount_sol=0.5,
        user_pubkey="Gx7...",
        keypair_bytes=b'...',
        slippage_bps=500,  # 5%
    )
"""
import logging
import time
import base64
import requests

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.commitment_config import CommitmentLevel
from solders.rpc.responses import SendTransactionPreflightFailure
from solders.rpc.config import RpcSendTransactionConfig

from bot import helius

logger = logging.getLogger(__name__)

JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP = "https://quote-api.jup.ag/v6/swap"

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# ── Quote ──────────────────────────────────────────────────────────────

def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 500,
) -> dict | None:
    """
    Get a swap quote from Jupiter v6.

    Args:
        input_mint: Token mint to swap FROM (use SOL_MINT for SOL)
        output_mint: Token mint to swap TO
        amount_lamports: Amount in lamports (for SOL) or smallest unit (for tokens)
        slippage_bps: Slippage tolerance in basis points (500 = 5%)

    Returns:
        Quote response dict with route info, or None on failure.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": False,
        "asLegacyTransaction": False,
    }
    try:
        resp = requests.get(JUPITER_QUOTE, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.debug("Jupiter quote failed: %s", e)
        return None


def _format_quote_summary(quote: dict, is_buy: bool) -> str:
    """Format a quote dict into a readable summary string."""
    in_amount = int(quote.get("inAmount", 0))
    out_amount = int(quote.get("outAmount", 0))
    price_impact = float(quote.get("priceImpactPct", 0))
    route_plan = quote.get("routePlan", [])

    if is_buy:
        in_sol = in_amount / LAMPORTS_PER_SOL
        # out_amount is in token decimals — we show raw for now
        return (
            f"📊 <b>Quote Preview</b>\n"
            f"Input: ◎ {in_sol:.6f} SOL\n"
            f"Output: {out_amount} tokens\n"
            f"Price impact: {price_impact:.2f}%\n"
            f"Routes: {len(route_plan)}"
        )
    else:
        return (
            f"📊 <b>Quote Preview</b>\n"
            f"Input: {in_amount} tokens\n"
            f"Output: ◎ {out_amount / LAMPORTS_PER_SOL:.6f} SOL\n"
            f"Price impact: {price_impact:.2f}%\n"
            f"Routes: {len(route_plan)}"
        )


# ── Swap transaction building ──────────────────────────────────────────

def get_swap_transaction(quote_response: dict, user_pubkey: str) -> str | None:
    """
    Get a base64-encoded serialized transaction from Jupiter swap API.

    Args:
        quote_response: The full quote dict from get_quote()
        user_pubkey: The user's Solana wallet public key string

    Returns:
        Base64-encoded transaction string, or None on failure.
    """
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
        "computeUnitPriceMicroLamports": "auto",
    }
    try:
        resp = requests.post(JUPITER_SWAP, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("swapTransaction")
    except requests.RequestException as e:
        logger.debug("Jupiter swap tx failed: %s", e)
        return None


# ── Tx submission ──────────────────────────────────────────────────────

def submit_transaction(base64_tx: str, keypair_bytes: bytes) -> str | None:
    """
    Sign and submit a serialized transaction to Solana.

    Args:
        base64_tx: Base64-encoded transaction from Jupiter
        keypair_bytes: Full 64-byte secret key for signing

    Returns:
        Transaction signature string on success, None on failure.
    """
    try:
        tx_bytes = base64.b64decode(base64_tx)
        keypair = Keypair.from_bytes(keypair_bytes)

        # Deserialize as VersionedTransaction
        tx = VersionedTransaction.from_bytes(tx_bytes)
        # Sign with user's keypair
        sig = tx.sign([keypair])

        # Send to RPC
        url = helius.HELIUS_RPC or helius.SOLANA_RPC
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(tx)).decode(),
                {
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "encoding": "base64",
                    "maxRetries": 3,
                },
            ],
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()

        if "result" in result:
            tx_sig = result["result"]
            logger.info("Transaction submitted: %s", tx_sig)
            return tx_sig
        else:
            error = result.get("error", {})
            logger.error("Transaction failed: %s", error.get("message", str(error)))
            return None

    except Exception as e:
        logger.error("Submit tx error: %s", e)
        return None


def check_transaction_status(tx_signature: str) -> dict:
    """
    Check the status of a submitted transaction.

    Returns dict with keys: confirmed (bool), slot (int), error (str or None).
    """
    url = helius.HELIUS_RPC or helius.SOLANA_RPC
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[tx_signature]],
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data:
            statuses = data["result"].get("value", [])
            if statuses and statuses[0]:
                s = statuses[0]
                return {
                    "confirmed": s.get("confirmationStatus") == "confirmed",
                    "slot": s.get("slot", 0),
                    "error": s.get("err", None),
                }
        return {"confirmed": False, "slot": 0, "error": "not found"}
    except requests.RequestException as e:
        return {"confirmed": False, "slot": 0, "error": str(e)}


# ── High-level buy/sell helpers ────────────────────────────────────────

def buy_token(
    token_address: str,
    amount_sol: float,
    user_pubkey: str,
    keypair_bytes: bytes,
    slippage_bps: int = 500,
) -> str | None:
    """
    Buy a token using SOL.

    Args:
        token_address: Token mint address to buy
        amount_sol: Amount of SOL to spend (e.g. 0.5)
        user_pubkey: User's wallet public key
        keypair_bytes: Full 64-byte keypair for signing
        slippage_bps: Slippage tolerance in basis points

    Returns:
        Transaction signature on success, None on failure.
    """
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    if amount_lamports <= 0:
        logger.error("Buy amount too small: %f SOL", amount_sol)
        return None

    # 1. Get quote
    quote = get_quote(SOL_MINT, token_address, amount_lamports, slippage_bps)
    if not quote:
        logger.error("No quote available for buy %s", token_address[:8])
        return None

    # 2. Get swap transaction
    base64_tx = get_swap_transaction(quote, user_pubkey)
    if not base64_tx:
        logger.error("Failed to get swap tx for buy %s", token_address[:8])
        return None

    # 3. Submit
    sig = submit_transaction(base64_tx, keypair_bytes)
    return sig


def sell_token(
    token_address: str,
    amount_tokens: int,
    user_pubkey: str,
    keypair_bytes: bytes,
    slippage_bps: int = 500,
) -> str | None:
    """
    Sell tokens back for SOL.

    Args:
        token_address: Token mint address to sell
        amount_tokens: Amount in smallest token unit (with decimals)
        user_pubkey: User's wallet public key
        keypair_bytes: Full 64-byte keypair for signing
        slippage_bps: Slippage tolerance in basis points

    Returns:
        Transaction signature on success, None on failure.
    """
    if amount_tokens <= 0:
        logger.error("Sell amount too small: %d", amount_tokens)
        return None

    # 1. Get quote (token -> SOL)
    quote = get_quote(token_address, SOL_MINT, amount_tokens, slippage_bps)
    if not quote:
        logger.error("No quote available for sell %s", token_address[:8])
        return None

    # 2. Get swap transaction
    base64_tx = get_swap_transaction(quote, user_pubkey)
    if not base64_tx:
        logger.error("Failed to get swap tx for sell %s", token_address[:8])
        return None

    # 3. Submit
    sig = submit_transaction(base64_tx, keypair_bytes)
    return sig


def get_token_decimals(mint_address: str) -> int:
    """Fetch token decimals from Solana chain."""
    url = helius.HELIUS_RPC or helius.SOLANA_RPC
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [mint_address],
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data:
            return data["result"]["value"]["decimals"]
    except Exception:
        pass
    return 6  # default for most Solana tokens