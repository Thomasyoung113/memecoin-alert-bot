"""Offline check: back-button navigation logic (stubs solders/base58)."""
import sys, types

for name, attrs in {
    "solders": {"__path__": []},
    "solders.keypair": {"Keypair": type("Keypair", (), {})},
    "solders.transaction": {"VersionedTransaction": type("VersionedTransaction", (), {})},
    "solders.commitment_config": {"CommitmentLevel": type("CommitmentLevel", (), {})},
    "solders.rpc": {"__path__": []},
    "solders.rpc.responses": {"SendTransactionPreflightFailure": type("S", (), {})},
    "solders.rpc.config": {"RpcSendTransactionConfig": type("R", (), {})},
    "base58": {"b58encode": lambda b: b"", "b58decode": lambda s: b""},
}.items():
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m

sys.path.insert(0, '/root/alert-bot')

from bot.listener import _with_back, _main_menu_markup, _auto_menu_markup

kb = _with_back([[{"text": "X", "callback_data": "x:1"}]])
assert kb["inline_keyboard"][-1][0]["callback_data"] == "nav:home"
assert len(kb["inline_keyboard"]) == 2

kb_auto = _with_back([[{"text": "Y", "callback_data": "y"}]], target="auto")
assert kb_auto["inline_keyboard"][-1][0]["callback_data"] == "auto:menu"
kb_sub = _with_back([[{"text": "Z", "callback_data": "z"}]], target="sub")
assert kb_sub["inline_keyboard"][-1][0]["callback_data"] == "sub:menu"

original = [[{"text": "A", "callback_data": "a"}]]
_with_back(original)
assert len(original) == 1, "caller list must not be mutated"

mm = _main_menu_markup()
flat = [b["callback_data"] for row in mm["inline_keyboard"] for b in row]
assert {"auto:menu", "pos:list", "sub:menu", "promo:ask",
        "nav:wallet", "nav:balance"} <= set(flat)

am = _auto_menu_markup({"buy_amount_sol": 0.25, "is_enabled": True})
assert am["inline_keyboard"][-1][0]["callback_data"] == "nav:home"
assert any("✅" in b["text"] and b["callback_data"] == "auto:amt_0.25"
           for row in am["inline_keyboard"] for b in row)

print("back-button nav offline checks: ALL PASS")
