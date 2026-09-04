"""
billing.py — Monetization engine (Phase 4).

Owns everything subscription-related:
  - join gate: users must be in the gate channel before /start unlocks
  - trials: 3 days full access, created on first /start
  - promo codes: 10 single-use 10-char codes generated daily and DM'd to
    the owner; redeeming grants a free week
  - SOL deposit invoices: ephemeral wallet per invoice, chain watcher,
    pro-rata credit below the tolerance threshold, treasury sweep
  - Telegram Stars: pre_checkout (answered <10s) + successful_payment
  - background jobs: expiry warnings, grace transitions, daily promos,
    daily reminder, weekly top call, invoice sweep

Funds-safety rule enforced everywhere: sells / TP-SL / wallet management
are NEVER gated. Only buys (manual + auto) and the alert stream are Pro.
"""
import html
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    GATE_CHANNEL_ID, PRO_CHANNEL_ID,
    TRIAL_HOURS, GRACE_HOURS,
    SUB_PRICE_WEEK_SOL, SUB_PRICE_MONTH_SOL,
    SUB_PRICE_WEEK_STARS, SUB_PRICE_MONTH_STARS,
    MIN_SOL_PRICE_RATIO, PROMO_CODES_PER_DAY, PROMO_DURATION_DAYS,
    PROMO_CODE_LEN, PROMO_GEN_HOUR_UTC, SIGNAL_HOUR_UTC,
    FREE_TOP_CALL_DAYS, PROMO_RATE_LIMIT_PER_DAY,
    TREASURY_WALLET_ADDRESS, INVOICE_TTL_MINUTES,
    WALLET_ENCRYPTION_KEY,
)

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _tg(method: str, payload: dict | None = None, timeout: int = 10):
    """Bot API call helper. Returns result or None."""
    try:
        resp = requests.post(f"{_API}/{method}", json=payload or {}, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            logger.debug("TG %s failed: %s", method, data.get("description"))
            return None
        return data.get("result")
    except requests.RequestException as e:
        logger.debug("TG %s error: %s", method, e)
        return None


# ── Join gate (must be in @everyday_aaii) ─────────────────────────────

_member_cache: dict[int, tuple[float, bool]] = {}
_member_cache_lock = threading.Lock()
_MEMBER_TTL = 3600  # 1h


def is_channel_member(telegram_id: int) -> bool:
    """True if the user is in the gate channel. Cached 1h."""
    now = time.time()
    with _member_cache_lock:
        hit = _member_cache.get(telegram_id)
        if hit and now - hit[0] < _MEMBER_TTL:
            return hit[1]
    from bot.helius import _rpc_call  # noqa: F401 (unused, keep imports local)
    status = None
    if TELEGRAM_BOT_TOKEN:
        result = _tg("getChatMember",
                     {"chat_id": GATE_CHANNEL_ID, "user_id": telegram_id})
        if result:
            status = result.get("status")
    ok = status in ("member", "administrator", "creator", "restricted")
    with _member_cache_lock:
        _member_cache[telegram_id] = (now, ok)
    return ok


def join_gate_message() -> str:
    return (
        "🔒 <b>One step before you enter</b>\n\n"
        f"Join our channel to unlock GEMBOT:\n"
        f"👉 t.me/{GATE_CHANNEL_ID.lstrip('@')}\n\n"
        "Then tap the button below."
    )


def join_gate_markup() -> dict:
    return {"inline_keyboard": [[
        {"text": "📢 Join Channel", "url": f"https://t.me/{GATE_CHANNEL_ID.lstrip('@')}"},
        {"text": "✅ I Joined", "callback_data": "gate:check"},
    ]]}


# ── Trials ────────────────────────────────────────────────────────────

def start_trial(user_id: int) -> dict:
    """Create the trial subscription (idempotent). Returns the sub row."""
    from bot.models import ensure_subscription, get_subscription, execute, close_cursor, commit
    now = datetime.now(timezone.utc)
    sub = ensure_subscription(user_id)
    if sub.get("tier") == "trial" and not sub.get("expires_at"):
        c = execute("""
            UPDATE subscriptions
            SET expires_at = %s, grace_until = %s, updated_at = %s
            WHERE id = %s
        """, ((now + timedelta(hours=TRIAL_HOURS)).isoformat(),
              (now + timedelta(hours=TRIAL_HOURS + GRACE_HOURS)).isoformat(),
              now.isoformat(), sub["id"]))
        close_cursor(c)
        commit()
    return get_subscription(user_id)


def subscription_status_line(user_id: int) -> str:
    """One-line status for the /start screen."""
    from bot.models import get_subscription
    sub = get_subscription(user_id)
    if not sub:
        return "🔴 No subscription yet"
    tier = sub.get("tier")
    status = sub.get("status")
    if status == "expired":
        return "⚪ Free tier — 1 top call/week"
    if tier == "trial" and sub.get("expires_at"):
        left = _parse(sub["expires_at"]) - datetime.now(timezone.utc)
        hours = max(0, int(left.total_seconds() // 3600))
        return f"🟢 Trial active — {hours}h left"
    if tier == "pro" and sub.get("expires_at"):
        left = _parse(sub["expires_at"]) - datetime.now(timezone.utc)
        hours = max(0, int(left.total_seconds() // 3600))
        return f"💎 Pro active — {hours}h left"
    return "⚪ Free tier — 1 top call/week"


def _parse(dtval):
    if isinstance(dtval, datetime):
        dt = dtval
    else:
        dt = datetime.fromisoformat(str(dtval))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Promo codes ───────────────────────────────────────────────────────

def generate_daily_promos() -> list[str]:
    """Create today's batch and DM it to the owner. Returns codes."""
    from bot.models import create_promo_batch, kv_get, kv_set
    from bot.jupiter import SOL_MINT  # noqa: F401
    today = datetime.now(timezone.utc).date().isoformat()
    if kv_get("last_promo_gen") == today:
        return []
    codes = create_promo_batch(PROMO_CODES_PER_DAY, PROMO_DURATION_DAYS, today)
    if not codes:
        return []
    kv_set("last_promo_gen", today)
    lines = [f"🎟 <b>GEMBOT promo codes — {today} (UTC)</b>",
             f"Single use · expires 23:59 UTC today · {PROMO_DURATION_DAYS}-day Pro each:", ""]
    lines += [f"<code>{c}</code>" for c in codes]
    lines += ["", "Drop them anywhere. First to redeem each wins."]
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines),
                            "parse_mode": "HTML"})
    logger.info("Generated %d promo codes for %s", len(codes), today)
    return codes


_promo_attempts: dict[int, list[str]] = {}  # telegram_id -> [dates]


def promo_allowed(telegram_id: int) -> bool:
    """Max N redemption attempts per user per day (anti-brute-force)."""
    today = datetime.now(timezone.utc).date().isoformat()
    days = _promo_attempts.get(telegram_id, [])
    days = [d for d in days if d == today]
    if len(days) >= PROMO_RATE_LIMIT_PER_DAY:
        _promo_attempts[telegram_id] = days
        return False
    days.append(today)
    _promo_attempts[telegram_id] = days
    return True


def redeem(code: str, telegram_id: int) -> tuple[bool, str]:
    from bot.models import redeem_promo
    ok, msg, _days = redeem_promo(code, telegram_id)
    return ok, msg


# ── SOL deposit invoices ──────────────────────────────────────────────

def create_invoice(user_id: int, plan: str) -> dict | None:
    """Mint an ephemeral deposit wallet + invoice. Returns display info."""
    from bot.models import create_deposit_invoice
    from bot.wallet import generate_wallet
    if plan not in ("week", "month"):
        return None
    amount = SUB_PRICE_WEEK_SOL if plan == "week" else SUB_PRICE_MONTH_SOL
    pubkey, encrypted, _raw = generate_wallet()
    inv_id = create_deposit_invoice(user_id, plan, amount, pubkey, encrypted,
                                    INVOICE_TTL_MINUTES)
    if not inv_id:
        return None
    return {
        "invoice_id": inv_id,
        "address": pubkey,
        "amount": amount,
        "plan": plan,
        "ttl_minutes": INVOICE_TTL_MINUTES,
    }


def _sweep_to_treasury(encrypted_privkey: str, min_balance: float = 0.002) -> str | None:
    """Send the invoice wallet's balance (above rent) to the treasury."""
    from bot.wallet import decrypt_private_key
    from bot.jupiter import LAMPORTS_PER_SOL
    if not TREASURY_WALLET_ADDRESS:
        logger.warning("TREASURY_WALLET_ADDRESS not set — skip sweep")
        return None
    try:
        from solders.keypair import Keypair
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction
        from solders.pubkey import Pubkey
        from bot.wallet import get_sol_balance
        kp = Keypair.from_bytes(decrypt_private_key(encrypted_privkey))
        balance = get_sol_balance(str(kp.pubkey()))
        if balance <= min_balance:
            return None
        send_lamports = int((balance - min_balance) * LAMPORTS_PER_SOL)
        if send_lamports <= 0:
            return None
        # Legacy transactions REQUIRE a fresh blockhash — without it every
        # send fails with "Blockhash not found".
        from bot.helius import _rpc_call
        bh = _rpc_call("getLatestBlockhash", [])
        if not bh or "result" not in bh:
            logger.warning("Sweep: could not fetch blockhash")
            return None
        blockhash = (bh["result"]["value"]["blockhash"])
        ix = transfer(TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=Pubkey.from_string(TREASURY_WALLET_ADDRESS),
            lamports=send_lamports))
        tx = Transaction.new_signed_with_blockhash([ix], kp.pubkey(), [kp],
                                                   blockhash)
        import base64
        result = _rpc_call("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                               {"encoding": "base64"}])
        if result and "result" in result:
            return result["result"]
        logger.warning("Sweep tx rejected: %s", result)
    except Exception:
        logger.exception("Sweep failed")
    return None


def check_pending_invoices() -> list[dict]:
    """Chain watcher: credit invoices whose wallet got enough SOL."""
    from bot.models import (
        get_pending_invoices, mark_invoice_paid, activate_subscription,
        record_payment, expire_stale_invoices, get_telegram_id,
    )
    from bot.wallet import get_sol_balance
    credited = []
    # expire stale ones (30-min grace past TTL so late payments still land)
    for stale in expire_stale_invoices():
        tid = get_telegram_id(stale["user_id"])
        if tid:
            _tg("sendMessage", {
                "chat_id": tid,
                "text": "⌛ Your SOL invoice expired. Tap 💎 Subscribe for a fresh address.",
            })
    for inv in get_pending_invoices():
        try:
            addr = inv["receive_address"]
            balance = get_sol_balance(addr)
            expected = float(inv["expected_sol"] or 0)
            if expected <= 0 or balance < expected * MIN_SOL_PRICE_RATIO:
                continue
            plan = inv["plan"] or "week"
            plan_days = 7 if plan == "week" else 30
            if balance < expected:
                # partial: pro-rata at the PLAN's own daily rate, capped at
                # the plan's full allotment (underpaying never beats paying).
                daily = expected / plan_days
                days = min(plan_days, max(1, int(balance / daily)))
                sig, sender = _find_deposit_tx(addr, expected)
                row = mark_invoice_paid(inv["id"], sig or "partial")
                if row:
                    uid, _plan = row
                    activate_subscription(uid, days, source="sol", plan="pro_rata")
                    record_payment(uid, "sol", balance, plan="pro_rata",
                                   sol_tx_sig=sig)
                    tid = get_telegram_id(uid)
                    credited.append({"telegram_id": tid, "days": days,
                                     "plan": "pro_rata"})
                    _sweep_to_treasury(inv["encrypted_privkey"])
                continue
            sig, sender = _find_deposit_tx(addr, expected)
            row = mark_invoice_paid(inv["id"], sig or "onchain")
            if not row:
                continue
            uid, plan = row
            days = plan_days
            activate_subscription(uid, days, source="sol", plan=plan)
            record_payment(uid, "sol", balance, plan=plan, sol_tx_sig=sig)
            tx = _sweep_to_treasury(inv["encrypted_privkey"])
            if tx:
                logger.info("Swept invoice wallet %s -> treasury (%s)", addr[:8], tx[:12])
            tid = get_telegram_id(uid)
            credited.append({"telegram_id": tid, "days": days, "plan": plan})
        except Exception:
            logger.exception("Invoice check failed for %s", inv.get("id"))
    return credited


def _find_deposit_tx(address: str, min_sol: float):
    """Find the latest funding tx on an invoice wallet.

    Returns (signature, sender) or (None, None) — used so payments are
    recorded with REAL tx signatures + sender addresses (refundability),
    not placeholder strings."""
    from bot.helius import _rpc_call
    sigs = _rpc_call("getSignaturesForAddress", [
        address, {"limit": 10}])
    if not sigs or "result" not in sigs:
        return None, None
    for s in sigs["result"]:
        if s.get("err"):
            continue
        tx = _rpc_call("getTransaction", [
            s["signature"], {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 0}])
        try:
            meta = tx["result"]["meta"]
            if meta.get("err"):
                continue
            keys = (tx["result"]["transaction"]["message"]
                    ["accountKeys"])
            payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
            delta = (meta["postBalances"][0] - meta["preBalances"][0])
            if delta >= min_sol * 1_000_000_000 * 0.9:
                return s["signature"], payer
        except (TypeError, KeyError, IndexError):
            continue
    return None, None


# ── Telegram Stars ────────────────────────────────────────────────────

STARS_PLANS = {
    "week": {"stars": SUB_PRICE_WEEK_STARS, "days": 7},
    "month": {"stars": SUB_PRICE_MONTH_STARS, "days": 30},
}


def send_stars_invoice(telegram_id: int, plan: str) -> bool:
    """Send a Stars invoice for the chosen plan."""
    plan_info = STARS_PLANS.get(plan)
    if not plan_info:
        return False
    result = _tg("sendInvoice", {
        "chat_id": telegram_id,
        "title": f"GEMBOT Pro — {plan.title()}",
        "description": f"All calls in real time + 24/7 auto-trading ({plan})",
        "payload": f"pro_{plan}",
        "currency": "XTR",
        "prices": [{"label": f"Pro {plan}", "amount": plan_info["stars"]}],
    })
    return result is not None


def handle_pre_checkout_query(pq: dict) -> None:
    """Answer within 10s: validate payload AND that the buyer is
    registered, then approve or reject. Money is only taken after ok=True,
    so anything we can cheaply validate must happen here."""
    pq_id = pq.get("id")
    payload = pq.get("invoice_payload", "")
    ok = payload in (f"pro_{p}" for p in STARS_PLANS)
    if ok:
        from bot.models import get_user_id_by_telegram
        buyer = pq.get("from", {}).get("id")
        if not buyer or not get_user_id_by_telegram(buyer):
            ok = False  # unregistered buyer — reject before charging
    if TELEGRAM_BOT_TOKEN:
        _tg("answerPreCheckoutQuery", {"pre_checkout_query_id": pq_id,
                                       "ok": ok})


def handle_successful_payment(telegram_id: int, msg: dict) -> None:
    """Grant Pro + receipt + Pro-channel invite.

    Ordering: ACTIVATE FIRST, then record — if the process dies between
    the two, the user has Pro but the payment row is missing (annoying,
    fixable) instead of having paid with nothing granted (unfixable).
    Genuinely idempotent: record_payment dedupes on charge id, and if it
    returns an existing id we skip re-activation (Telegram redelivery)."""
    from bot.models import activate_subscription, record_payment, get_user_id_by_telegram
    sp = msg.get("successful_payment", {})
    charge_id = sp.get("telegram_payment_charge_id")
    payload = sp.get("invoice_payload", "")
    plan = payload.replace("pro_", "") if payload.startswith("pro_") else "week"
    plan_info = STARS_PLANS.get(plan, STARS_PLANS["week"])
    uid = get_user_id_by_telegram(telegram_id)
    if not uid:
        logger.error("Stars payment from unregistered user %s — refunded via "
                     "Telegram? Grant manually after /start.", telegram_id)
        send_to_owner(f"⚠️ Stars payment from unknown user {telegram_id} "
                      f"(charge {charge_id}). They must /start, then contact you.")
        return
    pid = record_payment(uid, "stars", plan_info["stars"], currency="XTR",
                         plan=plan, provider_charge_id=charge_id)
    if pid is None:
        return  # duplicate charge id — already granted
    activate_subscription(uid, plan_info["days"], source="stars", plan=plan)
    ok = _tg("sendMessage", {"chat_id": telegram_id, "parse_mode": "HTML", "text":
        f"💎 <b>Pro activated — {plan_info['days']} days!</b>\n\n"
        "⚡ Every call, real time\n🤖 Auto-trading 24/7\n"
        "Use /auto on to arm the bot."})
    if not ok:
        send_to_owner(f"⚠️ Pro activated for user {uid} but the receipt DM "
                      "failed — they may not know.")
    _send_pro_channel_invite(telegram_id, uid)


def send_to_owner(text: str) -> None:
    """DM the owner (TELEGRAM_CHAT_ID) — for money-path alerts."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "parse_mode": "HTML"})


def run_preflight():
    """Startup checks that SHOUT about degraded features.

    Without this, silent breakages kill the funnel:
    - bot not admin of the gate channel -> EVERY /start is locked out
    - PRO_CHANNEL_ID unset -> paid users never get their invite
    - TREASURY_WALLET_ADDRESS unset -> SOL accumulates in invoice wallets
    """
    problems, warnings = [], []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN unset — Telegram is dead")
        return problems, warnings

    me = _tg("getMe")
    if not me:
        problems.append("getMe failed — token invalid or API unreachable")
        return problems, warnings

    # Gate channel: bot must be able to see members
    member = _tg("getChatMember", {"chat_id": GATE_CHANNEL_ID,
                                   "user_id": me["id"]})
    if not member or member.get("status") not in ("administrator", "creator"):
        problems.append(
            f"Bot is NOT admin of gate channel {GATE_CHANNEL_ID} — "
            "/start is LOCKED for everyone. Add the bot as admin there.")
    # Pro channel: invites require admin too
    if PRO_CHANNEL_ID:
        pmember = _tg("getChatMember", {"chat_id": PRO_CHANNEL_ID,
                                        "user_id": me["id"]})
        if not pmember or pmember.get("status") not in ("administrator", "creator"):
            problems.append(
                f"Bot is NOT admin of PRO_CHANNEL_ID {PRO_CHANNEL_ID} — "
                "paid users will not receive channel invites.")
    else:
        warnings.append("PRO_CHANNEL_ID unset — no official Pro channel")
    if not TREASURY_WALLET_ADDRESS:
        warnings.append("TREASURY_WALLET_ADDRESS unset — SOL from paid "
                        "invoices will pile up in invoice wallets")
    if not WALLET_ENCRYPTION_KEY:
        warnings.append("WALLET_ENCRYPTION_KEY unset — wallet creation disabled")

    for p in problems:
        logger.critical("PREFLIGHT: %s", p)
    for w in warnings:
        logger.warning("PREFLIGHT: %s", w)
    if TELEGRAM_CHAT_ID:
        msg = "🚦 <b>Startup preflight</b>\n" + "\n".join(
            [f"❌ {p}" for p in problems] + [f"⚠️ {w}" for w in warnings])
        if problems or warnings:
            _tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                                "parse_mode": "HTML"})
    return problems, warnings


def _send_pro_channel_invite(telegram_id: int, user_id: int) -> None:
    """Single-use invite to the official Pro channel, stored on the sub."""
    from bot.models import get_subscription, execute, close_cursor, commit
    if not PRO_CHANNEL_ID:
        return
    sub = get_subscription(user_id)
    if sub and sub.get("pro_channel_invite"):
        _tg("sendMessage", {"chat_id": telegram_id,
                            "text": f"📢 Pro channel: {sub['pro_channel_invite']}"})
        return
    link = _tg("createChatInviteLink", {
        "chat_id": PRO_CHANNEL_ID, "member_limit": 1, "name": f"user-{user_id}"})
    if not link:
        logger.warning("Could not create Pro invite (bot admin in %s?)", PRO_CHANNEL_ID)
        return
    invite_url = link.get("invite_link")
    if sub:
        c = execute("UPDATE subscriptions SET pro_channel_invite = %s WHERE id = %s",
                    (invite_url, sub["id"]))
        close_cursor(c)
        commit()
    _tg("sendMessage", {"chat_id": telegram_id,
                        "text": f"📢 <b>Official Pro channel:</b>\n{invite_url}"})


# ── Background jobs (billing thread) ──────────────────────────────────

def _daily_reminder_and_top_call():
    """Free tier: daily reminder; weekly: the top call of the week."""
    from bot.models import (
        kv_get, kv_set, get_telegram_ids_by_tier, execute, close_cursor,
        _dict_rows,
    )
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if kv_get("last_free_reminder") == today:
        return

    # Free tier = users whose subscription expired (tier free, status
    # expired — set together by the expiry transitions). NOT 'active'.
    free_ids = get_telegram_ids_by_tier(["free"], statuses=("expired",))
    if not free_ids:
        return
    kv_set("last_free_reminder", today)  # set AFTER audience is known

    # weekly pick: best resolved 2x call in the trailing week
    top = None
    week_ago = (now - timedelta(days=FREE_TOP_CALL_DAYS)).isoformat()
    if kv_get("last_top_call_date") and \
            (now - _parse(kv_get("last_top_call_date"))).days < FREE_TOP_CALL_DAYS:
        top = None  # not due yet
    else:
        c = execute("""
            SELECT id, token_address, symbol, alert_mcap, peak_mcap FROM alerts
            WHERE resolved = 1 AND hit_2x = 1 AND alert_time >= %s
            ORDER BY (peak_mcap / NULLIF(alert_mcap, 0)) DESC NULLS LAST
            LIMIT 1
        """, (week_ago,))
        rows = _dict_rows(c)
        close_cursor(c)
        top = rows[0] if rows else None

    for tid in free_ids:
        text = ("🎯 <b>Your free weekly call is ready</b>\n\n"
                "Pro members got every call this week — live, with 24/7 auto-trading.\n"
                "💎 <b>0.2 SOL/week · 0.5 SOL/month</b>\n"
                "🎟 Today's promo codes may be live on @thomas_young\n\n"
                "/subscribe to go Pro")
        if top:
            mult = (top.get("peak_mcap") or 0) / max(1, top.get("alert_mcap") or 1)
            sym = html.escape(str(top["symbol"]))
            text = (f"🎯 <b>THIS WEEK'S TOP CALL: ${sym}</b>\n"
                    f"Called at ${top['alert_mcap']:,.0f} → peaked "
                    f"${top.get('peak_mcap') or 0:,.0f} ({mult:.1f}x)\n\n" + text)
            text = text.replace("/subscribe to go Pro",
                                f"Chart: dexscreener.com/solana/{top['token_address']}\n\n"
                                "/subscribe to go Pro")
        _tg("sendMessage", {"chat_id": tid, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    if top:
        kv_set("last_top_call_date", now.isoformat())


def _expiry_jobs():
    """48h warning, grace flips, expired notices, Pro-channel cleanup."""
    from bot.models import (
        get_subscription, get_expired_in_grace, get_grace_expired,
        get_untouched_trial_expiry, set_48h_warned, get_telegram_id,
        execute, close_cursor, commit,
    )
    now = datetime.now(timezone.utc)
    # warn subs expiring within 48h that haven't been warned (pro only —
    # trials get their own expiry notice via get_untouched_trial_expiry)
    c = execute("""
        SELECT id, user_id, expires_at FROM subscriptions
        WHERE status = 'active' AND warned_48h = FALSE AND tier = 'pro'
          AND expires_at IS NOT NULL
          AND expires_at < %s AND expires_at > %s
    """, ((now + timedelta(hours=48)).isoformat(), now.isoformat()))
    rows = _dict_rows(c)
    close_cursor(c)
    for r in rows:
        tid = get_telegram_id(r["user_id"])
        if tid:
            _tg("sendMessage", {"chat_id": tid, "parse_mode": "HTML", "text":
                "⏳ <b>Pro ends in &lt;48h</b>\n\n"
                "Renew to keep every call + auto-trading:\n"
                "💎 0.2 SOL/week · 0.5 SOL/month\n\n/subscribe"})
        set_48h_warned(r["id"])

    # active → grace (buys lock, sells keep running)
    for r in get_expired_in_grace():
        tid = get_telegram_id(r["user_id"])
        if tid:
            _tg("sendMessage", {"chat_id": tid, "parse_mode": "HTML", "text":
                "⚠️ <b>Your Pro expired — 48h grace started.</b>\n\n"
                "🔓 Selling your positions still works.\n"
                "🔒 New buys are locked.\n\n/subscribe to restore Pro"})

    # grace → expired
    for r in get_grace_expired():
        tid = get_telegram_id(r["user_id"])
        if tid:
            _tg("sendMessage", {"chat_id": tid, "parse_mode": "HTML", "text":
                "🔒 <b>Pro access ended.</b>\n\n"
                "You keep: your wallet, /sell, /balance — your funds stay yours.\n"
                "💎 /subscribe any time to jump back in."})

    # untouched trials → expired
    for r in get_untouched_trial_expiry():
        tid = get_telegram_id(r["user_id"])
        if tid:
            _tg("sendMessage", {"chat_id": tid, "parse_mode": "HTML", "text":
                "⏱️ <b>Trial over.</b>\n\n"
                "You keep: 1 top call/week + manual trading + your wallet.\n"
                "💎 <b>0.2 SOL/week · 0.5 SOL/month</b>\n"
                "🎟 Promo codes drop daily on @thomas_young\n\n/subscribe"})


def _sweep_pro_channel():
    """Remove expired users from the Pro channel (bot must be admin there)."""
    from bot.models import get_telegram_ids_by_tier
    if not PRO_CHANNEL_ID:
        return
    expired = get_telegram_ids_by_tier(["free"], statuses=("expired",))
    for tid in expired:
        _tg("banChatMember", {"chat_id": PRO_CHANNEL_ID, "user_id": tid,
                              "until_date": int(time.time()) + 60})
        _tg("unbanChatMember", {"chat_id": PRO_CHANNEL_ID, "user_id": tid,
                                "only_if_banned": True})


def _process_invoices():
    """Credit paid invoices AND notify the payers (SOL path was silent)."""
    for credit in check_pending_invoices():
        tid = credit.get("telegram_id")
        days = credit.get("days", 0)
        plan = credit.get("plan", "week")
        if tid:
            _tg("sendMessage", {"chat_id": tid, "parse_mode": "HTML", "text":
                f"💎 <b>Payment received — Pro for {days} days!</b>\n\n"
                "⚡ Every call, real time\n🤖 Auto-trading 24/7\n"
                "Use /auto on to arm the bot."})
            from bot.models import get_user_id_by_telegram
            uid = get_user_id_by_telegram(tid)
            if uid:
                _send_pro_channel_invite(tid, uid)
        send_to_owner(f"💰 SOL invoice credited: {days}d {plan} for "
                      f"{tid or 'unknown user'}")


def billing_loop():
    """Daemon: runs the daily/weekly/nightly jobs. Safe to loop forever.

    Daily jobs are date-anchored (kv flags): they run once per UTC day,
    any time AFTER their scheduled hour — a restart or slow tick can
    delay them but never skip them."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour >= PROMO_GEN_HOUR_UTC:
                generate_daily_promos()  # self-guards via last_promo_gen
            if now.hour >= SIGNAL_HOUR_UTC:
                _daily_reminder_and_top_call()  # kv last_free_reminder
            _expiry_jobs()
            _process_invoices()
            if now.hour >= 4:  # quiet-hours cleanup
                _sweep_pro_channel_marked()
        except Exception:
            logger.exception("Billing loop error")
        time.sleep(300)


def _sweep_pro_channel_marked():
    """_sweep_pro_channel with a date anchor so it runs once per day."""
    from bot.models import kv_get, kv_set
    today = datetime.now(timezone.utc).date().isoformat()
    if kv_get("last_pro_sweep") == today:
        return
    _sweep_pro_channel()
    kv_set("last_pro_sweep", today)


def start_billing():
    t = threading.Thread(target=billing_loop, daemon=True, name="billing")
    t.start()
    logger.info("Billing thread started")
