"""
wallet.py — Solana wallet generation, encryption, and balance queries.

Uses solders for key generation and the existing Helius RPC for balance lookups.
Private keys are encrypted at rest with Fernet (AES-128-CBC + HMAC).
"""
import logging
import json

import requests
from solders.keypair import Keypair
import base58

from config import HELIUS_RPC, SOLANA_RPC, WALLET_ENCRYPTION_KEY

logger = logging.getLogger(__name__)

# ── Encryption ────────────────────────────────────────────────────────

_fernet = None


def _get_fernet():
    """Lazy-init Fernet cipher from the encryption key."""
    global _fernet
    if _fernet is not None:
        return _fernet
    if not WALLET_ENCRYPTION_KEY:
        logger.warning("WALLET_ENCRYPTION_KEY not set — wallet creation disabled")
        return None
    from cryptography.fernet import Fernet
    _fernet = Fernet(WALLET_ENCRYPTION_KEY.encode())
    return _fernet


def encrypt_private_key(raw_key: bytes) -> str:
    """Encrypt a raw private key bytes and return a base64 string."""
    f = _get_fernet()
    if not f:
        raise RuntimeError("Wallet encryption is not configured (set WALLET_ENCRYPTION_KEY)")
    return f.encrypt(raw_key).decode()


def decrypt_private_key(encrypted: str) -> bytes:
    """Decrypt an encrypted private key back to raw bytes."""
    f = _get_fernet()
    if not f:
        raise RuntimeError("Wallet encryption is not configured (set WALLET_ENCRYPTION_KEY)")
    return f.decrypt(encrypted.encode())


# ── Key generation ────────────────────────────────────────────────────

def generate_wallet() -> tuple[str, str, bytes]:
    """
    Generate a new Solana wallet.

    Returns:
        (public_key_str, encrypted_private_key_str, raw_seed_bytes)
    """
    kp = Keypair()
    pubkey = str(kp.pubkey())
    # solders Keypair secret() returns the full 64-byte secret key
    raw_key = bytes(kp.secret())
    encrypted = encrypt_private_key(raw_key)
    return pubkey, encrypted, raw_key


# ── RPC calls (reuses Helius/Solana RPC from config) ──────────────────

def _rpc_call(method: str, params: list) -> dict | None:
    """Make a JSON-RPC call to Solana."""
    url = HELIUS_RPC or SOLANA_RPC
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


def get_sol_balance(pubkey: str) -> float:
    """
    Get SOL balance for a wallet address.

    Returns SOL amount (not lamports). Returns 0.0 on error.
    """
    result = _rpc_call("getBalance", [pubkey])
    if result and "result" in result:
        lamports = result["result"].get("value", 0)
        return lamports / 1_000_000_000
    return 0.0


def get_token_balances(pubkey: str) -> list[dict]:
    """
    Get all SPL token balances for a wallet.

    Returns list of dicts:
      [{"mint": str, "amount": float, "decimals": int, "symbol": str or None}, ...]
    """
    result = _rpc_call("getTokenAccountsByOwner", [
        pubkey,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"},
    ])
    tokens = []
    if not result or "result" not in result:
        return tokens
    for account in result["result"].get("value", []):
        account_data = account.get("account", {}).get("data", {})
        parsed = account_data.get("parsed", {})
        info = parsed.get("info", {})
        token_amount = info.get("tokenAmount", {})
        ui_amount = float(token_amount.get("uiAmount", 0) or 0)
        if ui_amount > 0:
            mint = info.get("mint", "")
            tokens.append({
                "mint": mint,
                "amount": ui_amount,
                "decimals": token_amount.get("decimals", 0),
            })
    return tokens


def get_wallet_info(pubkey: str) -> dict:
    """
    Get wallet overview: SOL balance + top token balances.

    Returns:
      {"sol": float, "tokens": list[dict], "address": str}
    """
    sol = get_sol_balance(pubkey)
    tokens = get_token_balances(pubkey)
    # Sort by amount descending, take top 10
    tokens.sort(key=lambda t: t["amount"], reverse=True)
    return {
        "address": pubkey,
        "sol": sol,
        "tokens": tokens[:10],
    }