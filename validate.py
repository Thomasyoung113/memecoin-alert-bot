"""Validation runner v2: compile + import + DB smoke (with offline stubs)."""
import sys
sys.path.insert(0, '/root/alert-bot')
sys.path.insert(0, '/root')

results = []

def check(name, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
    except Exception as e:
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"))

def compile_all():
    import py_compile, glob
    files = ['/root/alert-bot/main.py', '/root/alert-bot/config.py'] \
        + glob.glob('/root/alert-bot/bot/*.py') \
        + glob.glob('/root/alert-bot/dashboard/*.py')
    for f in files:
        py_compile.compile(f, doraise=True)

check("compile", compile_all)

def import_all():
    import config
    import bot.models, bot.scanner, bot.checker, bot.tracker
    import bot.insider_tracker, bot.learner, bot.helius
    import bot.pnl_card, bot.telegram, bot.milestones
    import bot.billing
    import dashboard.server

check("imports (no-solders set)", import_all)

def import_billing_logic():
    """Billing functions that don't touch solders — verify core logic imports."""
    from bot.billing import (
        STARS_PLANS, promo_allowed, join_gate_message, join_gate_markup,
        subscription_status_line, billing_loop, start_billing,
    )
    from bot.models import (
        get_subscription, ensure_subscription, activate_subscription, is_pro,
        is_paid_pro, create_promo_batch, redeem_promo, create_deposit_invoice,
        get_pending_invoices, mark_invoice_paid, record_payment,
        get_expired_in_grace, get_grace_expired, get_telegram_ids_by_tier,
        kv_get, kv_set,
    )
    # logic sanity checks
    assert set(STARS_PLANS) == {"week", "month"}
    assert promo_allowed(999999999) is True
    msg = join_gate_message()
    assert "everyday_aaii" in msg
    kb = join_gate_markup()
    assert kb["inline_keyboard"][0][1]["callback_data"] == "gate:check"
    # promo alphabet has no ambiguous chars
    import bot.models as m
    assert not (set("0O1I") & set(m._PROMO_ALPHABET))

check("billing logic (offline)", import_billing_logic)

def import_trading():
    import bot.wallet, bot.jupiter, bot.listener, bot.auto_trader

check("imports (trading path — needs solders)", import_trading)

def db_smoke():
    from dotenv import load_dotenv
    import os
    load_dotenv('/root/alert-bot/.env')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL not set in .env (configure on Railway)')
    from bot.models import init_db, execute, close_cursor, _scalar, commit
    init_db()
    for table in ('auto_trade_config', 'positions', 'subscriptions', 'payments',
                  'promo_codes', 'deposit_invoices'):
        c = execute(f"SELECT COUNT(*) FROM {table}")
        _scalar(c)
        close_cursor(c)

check("db smoke — needs DATABASE_URL", db_smoke)

print()
ok = True
for name, status, err in results:
    line = f"{status:4} {name}"
    if err:
        line += f"  —  {err}"
    print(line)
    if status == "FAIL":
        ok = False
sys.exit(0 if ok else 1)
