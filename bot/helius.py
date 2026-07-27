"""
Helius RPC client — fetches on-chain data for smart money analysis.
Uses public Solana RPC (free, no key needed for basic queries).
"""
import logging
import requests

logger = logging.getLogger(__name__)

# Public Solana RPC endpoint (or use Helius with API key for higher limits)
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Helius free tier — sign up at https://helius.xyz to get a key
# then set HELIUS_API_KEY in .env
HELIUS_RPC = None  # Will be set if API key is available


def init_helius(api_key: str = ""):
    """Initialize Helius RPC with an API key (optional, for higher rate limits)."""
    global HELIUS_RPC
    if api_key:
        HELIUS_RPC = f"https://rpc.helius.xyz/?api-key={api_key}"
        logger.info("Helius RPC initialized")
    else:
        logger.info("Using public Solana RPC (rate-limited)")


def _rpc_call(method: str, params: list, use_helius: bool = False) -> dict | None:
    """Make a JSON-RPC call to Solana."""
    url = HELIUS_RPC if (HELIUS_RPC and use_helius) else SOLANA_RPC
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.debug("RPC call %s failed: %s", method, e)
        return None


def get_signatures_for_token(mint_address: str, limit: int = 100) -> list[str]:
    """
    Get the earliest transaction signatures for a token mint.

    Returns a list of signatures (oldest first if paginated correctly).
    """
    # Get signatures for the token mint address, limited to `limit`
    result = _rpc_call("getSignaturesForAddress", [
        mint_address,
        {"limit": limit, "commitment": "finalized"},
    ])
    if result and "result" in result:
        # Signatures are returned newest-first, reverse for chronological
        sigs = [s["signature"] for s in result["result"]]
        sigs.reverse()
        return sigs
    return []


def get_transaction(signature: str) -> dict | None:
    """Get full transaction details for a signature."""
    result = _rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])
    if result and "result" in result:
        return result["result"]
    return None


def extract_buyers_from_tx(tx: dict) -> list[dict]:
    """
    Extract buyer wallet addresses from a transaction.
    Returns list of {"address": str, "amount": float, "token": str}.
    """
    buyers = []
    meta = tx.get("meta", {})
    if not meta:
        return buyers

    tx_json = tx.get("transaction", {}).get("message", {})
    instructions = tx_json.get("instructions", [])
    account_keys = tx_json.get("accountKeys", [])

    # Parse inner instructions for token transfers (swap sources)
    inner_instructions = meta.get("innerInstructions", [])
    for ii_group in inner_instructions:
        for ii in ii_group.get("instructions", []):
            if ii.get("program") != "spl-token":
                continue
            parsed = ii.get("parsed", {})
            if parsed.get("type") == "transfer":
                info = parsed.get("info", {})
                source = info.get("source", "")
                dest = info.get("destination", "")
                amount = int(info.get("amount", 0))

                # If destination is the user's wallet (not the AMM), it's a buy
                # For simplicity, we just record any transfer involving known token accounts
                buyers.append({
                    "source": source,
                    "dest": dest,
                    "amount": amount,
                })

    # Also try to get the fee payer as a buyer indicator
    fee_payer = account_keys[0] if account_keys else None
    if fee_payer:
        # The fee payer is usually the signer = the buyer
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        sol_change = (pre_balances[0] - post_balances[0]) / 1e9 if len(pre_balances) > 0 else 0
        if sol_change > 0:
            buyers.append({
                "address": fee_payer,
                "sol_spent": round(sol_change, 6),
            })

    return buyers


def get_early_buyers(token_address: str, depth: int = 100) -> list[str]:
    """
    Get the earliest buyer wallet addresses for a token.

    Fetches the first N transactions and extracts unique buyer addresses,
    preserving order (first buyer = first in list).

    Returns list of wallet addresses.
    """
    sigs = get_signatures_for_token(token_address, limit=depth)
    if not sigs:
        logger.debug("No signatures found for %s", token_address[:8])
        return []

    # Limit to first N signatures
    sigs = sigs[:depth]

    buyer_addresses = []
    seen = set()

    for sig in sigs:
        tx = get_transaction(sig)
        if not tx:
            continue

        # Get the fee payer (signer) — this is the buyer
        tx_json = tx.get("transaction", {}).get("message", {})
        account_keys = tx_json.get("accountKeys", [])
        if account_keys:
            # First account key is usually the fee payer / signer
            buyer = account_keys[0]
            if buyer not in seen and buyer != token_address:
                seen.add(buyer)
                buyer_addresses.append(buyer)

        if len(buyer_addresses) >= depth:
            break

    logger.info("Found %d early buyers for %s",
                len(buyer_addresses), token_address[:8])
    return buyer_addresses