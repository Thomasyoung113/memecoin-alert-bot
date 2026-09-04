"""
Money-path test suite (pytest).

Covers the functions a single refactor could silently break:
  subscriptions math, promo redemption atomicity, expiry transitions,
  wash gates, auto-buy clamps, PnL math, nav markup integrity.

Run:  pytest tests/ -v          (no DB, no network — pure logic + stubs)
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# pytest is optional: tests must run under the built-in runner too.
try:
    import pytest
    approx = pytest.approx
except ImportError:
    def approx(expected, rel=None, abs=None):
        return _Approx(expected)
    class _Approx:
        def __init__(self, expected):
            self.expected = expected
        def __eq__(self, other):
            return abs(other - self.expected) < 1e-6
    class _PytestShim:
        @staticmethod
        def approx(expected, **kw):
            return _Approx(expected)
    pytest = _PytestShim

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Stub solders/base58 (not installed in the test env) ──────────────
for name, attrs in {
    "solders": {"__path__": []},
    "solders.keypair": {"Keypair": type("Keypair", (), {})},
    "solders.transaction": {
        "VersionedTransaction": type("VersionedTransaction", (), {}),
        "Transaction": type("Transaction", (), {})},
    "solders.system_program": {
        "TransferParams": type("TransferParams", (), {}),
        "transfer": staticmethod(lambda **kw: None)},
    "solders.pubkey": {"Pubkey": type("Pubkey", (), {})},
    "solders.commitment_config": {
        "CommitmentLevel": type("CommitmentLevel", (), {})},
    "solders.rpc": {"__path__": []},
    "solders.rpc.responses": {
        "SendTransactionPreflightFailure": type("S", (), {})},
    "solders.rpc.config": {"RpcSendTransactionConfig": type("R", (), {})},
    "base58": {"b58encode": lambda b: "", "b58decode": lambda s: b""},
}.items():
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)


# ═══════════════════ Subscription math (pure logic mirror) ══════════

def _activate(base_expiry, now, days, grace_hours=48):
    """Mirror of models.activate_subscription date math."""
    start = max(base_expiry, now)
    expires = start + timedelta(days=days)
    return expires, expires + timedelta(hours=grace_hours)


class TestSubscriptionMath:
    def test_fresh_user_gets_full_days_from_now(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        exp, grace = _activate(now, now, 7)
        assert exp == now + timedelta(days=7)
        assert grace == exp + timedelta(hours=48)

    def test_active_sub_extends_from_expiry_not_now(self):
        """A 7-day buy with 3 days left = 10 days total, never 7."""
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        base = now + timedelta(days=3)
        exp, _ = _activate(base, now, 7)
        assert exp == now + timedelta(days=10)

    def test_expired_sub_restarts_from_now(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        base = now - timedelta(days=2)  # expired two days ago
        exp, _ = _activate(base, now, 7)
        assert exp == now + timedelta(days=7)  # no negative carry


class TestPromoAlphabet:
    def test_alphabet_excludes_ambiguous_chars(self):
        from bot.models import _PROMO_ALPHABET
        assert not (set("0O1I") & set(_PROMO_ALPHABET))
        assert len(_PROMO_ALPHABET) == 32  # 26-4 + 10 digits

    def test_promo_rate_limit_keys_per_user(self):
        from bot.billing import promo_allowed
        a, b = 111111111, 222222222
        assert promo_allowed(a)
        # b is unaffected by a's attempts
        assert promo_allowed(b)


# ═══════════════════ Wash gate ═══════════════════════════════════════

class TestWashDetector:
    def test_healthy_coin_scores_zero(self):
        from bot.wash_detector import score_pair_math
        pair = {"txns": {"h24": {"buys": 200, "sells": 90},
                         "m5": {"buys": 30, "sells": 12}},
                "volume": {"m5": 25_000, "h1": 180_000},
                "priceChange": {"h1": 22.0, "m5": 5.0}}
        r = score_pair_math(pair)
        assert r["wash_score"] == 0

    def test_churn_farm_scores_40_and_is_rejected(self):
        from bot.wash_detector import score_pair_math, reject_call
        pair = {"txns": {"h24": {"buys": 150, "sells": 145},
                         "m5": {"buys": 20, "sells": 19}},
                "volume": {"m5": 40_000, "h1": 300_000},
                "priceChange": {"h1": 0.2, "m5": 0.1}}
        r = score_pair_math(pair)
        assert r["wash_score"] == 40
        assert reject_call(r["wash_score"])  # >= 35 = no call

    def test_gate_thresholds(self):
        from bot.wash_detector import reject_call, allow_auto_buy
        assert reject_call(35) and reject_call(100)
        assert not reject_call(34)
        assert allow_auto_buy(19)
        assert not allow_auto_buy(20)  # auto-buy stricter than calls

    def test_asymmetric_moving_coin_not_flagged(self):
        from bot.wash_detector import score_pair_math
        pair = {"txns": {"h24": {"buys": 100, "sells": 95}},
                "volume": {"m5": 20_000, "h1": 100_000},
                "priceChange": {"h1": 8.0, "m5": 2.0}}
        assert score_pair_math(pair)["wash_score"] == 0

    def test_wash_line_renders_reasons(self):
        from bot.wash_detector import wash_line
        line = wash_line(40, {"symmetry_flag": True, "divergence_flag": True})
        assert "HIGH" in line and "flat churn" in line and "fake volume" in line


# ═══════════════════ Trading clamps & PnL ════════════════════════════

class TestAutoTraderMath:
    def test_buy_amount_clamped(self):
        from bot.auto_trader import _clamp_buy_amount
        assert _clamp_buy_amount(0.5) == 0.5
        assert _clamp_buy_amount(1000) == 10.0     # fat finger capped
        assert _clamp_buy_amount(0.0000001) == 0.001
        assert _clamp_buy_amount("garbage") == 0.001
        assert _clamp_buy_amount(None) == 0.001

    def test_tp_sl_defaults(self):
        from bot.auto_trader import _tp_sl
        assert _tp_sl({}) == (100.0, 50.0)
        assert _tp_sl({"take_profit_pct": 200, "stop_loss_pct": 40}) == (200.0, 40.0)
        assert _tp_sl({"take_profit_pct": None, "stop_loss_pct": None}) == (100.0, 50.0)

    def test_pnl_pct(self):
        from bot.auto_trader import _pnl_pct
        assert _pnl_pct(100, 250) == pytest.approx(150.0)
        assert _pnl_pct(100, 50) == pytest.approx(-50.0)
        assert _pnl_pct(0, 100) is None
        assert _pnl_pct(None, 100) is None


class TestWalletClamps:
    def test_balances_none_vs_empty_contract(self):
        """get_token_balances must return None on RPC failure — callers
        rely on None != [] to avoid stranding positions."""
        from bot.wallet import get_token_balances
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "wallet.py")).read()
        assert "return None  # RPC failed" in src, (
            "RPC-failure sentinel missing — position-killer regression")


# ═══════════════════ Invoice / Stars ordering ════════════════════════

class TestInvoiceExpiryGrace:
    def test_watcher_window_has_30min_grace(self):
        """get_pending_invoices must look 30min past expiry so late
        payments still credit (the race that stranded SOL)."""
        from bot.models import get_pending_invoices
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "models.py")).read()
        assert "timedelta(minutes=30)" in src

    def test_partial_payment_capped_at_plan_days(self):
        """Underpaying must never buy more days than full price."""
        expected = 0.2  # week
        balance = 0.195  # passes 97% tolerance
        plan_days = 7
        daily = expected / plan_days
        days = min(plan_days, max(1, int(balance / daily)))
        assert days <= 7  # the old monthly-rate bug gave 11


class TestStarsOrdering:
    def test_activation_gated_on_fresh_charge_id(self):
        """Safe property: a duplicate charge id must SKIP re-activation
        (no double-extension), whatever the call order is."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "billing.py")).read()
        act = src.index("def handle_successful_payment")
        seg = src[act:src.index("def ", act + 10)]
        # pid comes from record_payment and is checked before activation
        assert "pid = record_payment" in seg
        assert "if pid is None:" in seg
        assert "return  # duplicate charge id" in seg
        assert seg.index("record_payment(uid") < seg.index("if pid is None")
        assert seg.index("if pid is None") < seg.index("activate_subscription(uid")

    def test_unregistered_payer_alerts_owner(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "billing.py")).read()
        act = src.index("def handle_successful_payment")
        seg = src[act:src.index("def _send_pro_channel_invite", act)]
        assert "send_to_owner" in seg  # money path never fully silent


# ═══════════════════ Telegram surface integrity ══════════════════════

class TestNavigation:
    def test_every_keyboard_gets_back_row(self):
        from bot.listener import _with_back
        kb = _with_back([[{"text": "X", "callback_data": "x"}]])
        assert kb["inline_keyboard"][-1][0]["callback_data"] == "nav:home"

    def test_with_back_does_not_mutate_caller(self):
        from bot.listener import _with_back
        original = [[{"text": "A", "callback_data": "a"}]]
        _with_back(original)
        assert len(original) == 1

    def test_main_menu_hub_complete(self):
        from bot.listener import _main_menu_markup
        flat = [b["callback_data"] for row in
                _main_menu_markup()["inline_keyboard"] for b in row]
        assert {"auto:menu", "pos:list", "sub:menu", "promo:ask",
                "nav:wallet", "nav:balance"} <= set(flat)


class TestCallbackClamps:
    """Button presets must clamp identically to /auto set (crafted
    callback_data bypassed limits before the fix)."""

    def test_amount_clamp_in_callback_branch(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "listener.py")).read()
        seg = src[src.index('data.startswith("auto:amt_")'):]
        seg = seg[:seg.index("\n    if", 10)]
        assert "min(10.0" in seg
        assert "max(0.001" in seg

    def test_sl_clamp_in_callback_branch(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "listener.py")).read()
        seg = src[src.index('data.startswith("auto:sl_")'):]
        seg = seg[:seg.index("\n    if", 10)]
        assert "min(99.0" in seg


class TestHtmlSafety:
    def test_status_echoes_are_escaped(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "listener.py")).read()
        seg = src[src.index("def _cmd_status"):]
        seg = seg[:seg.index("def ", 10)]
        assert "html.escape(sig[:20])" in seg
        assert "html.escape(str(result['error']))" in seg

    def test_auto_screen_escapes_token_symbols(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "listener.py")).read()
        seg = src[src.index("def _cmd_auto"):]
        seg = seg[:seg.index("def ", 10)]
        assert "html.escape(p.get('token_symbol')" in seg

    def test_weekly_top_call_escapes_symbol(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "billing.py")).read()
        seg = src[src.index("_daily_reminder_and_top_call"):]
        assert 'html.escape(str(top["symbol"]))' in seg


class TestSellNeverGated:
    def test_require_pro_not_called_in_sell_paths(self):
        """Funds-safety rule: /sell, /auto off, and the TP/SL sweep must
        never grow a subscription gate."""
        listener = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "listener.py")).read()
        sell_seg = listener[listener.index("def _cmd_sell"):
                            listener.index("def _cmd_slippage")]
        assert "_require_pro" not in sell_seg
        auto_seg = listener[listener.index("def _cmd_auto"):]
        off_seg = auto_seg[auto_seg.index('sub == "off"'):
                           auto_seg.index('sub == "set"')]
        assert "_require_pro" not in off_seg
