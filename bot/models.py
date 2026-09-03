"""
Database layer — PostgreSQL via psycopg2.
Uses DATABASE_URL from config (set via Railway Postgres addon).
"""
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_conn = None
_conn_lock = threading.Lock()


def get_conn():
    """Get a singleton connection to PostgreSQL (lazy init)."""
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            _conn.autocommit = False
    return _conn


def execute(sql, params=None):
    """Thread-safe: get cursor under lock, execute SQL, return cursor.

    The lock is held only while executing (not while the caller fetches)
    so concurrent threads serialize statement preparation but still fetch
    from their own cursors. Callers must close the cursor and commit
    writes via commit() (also serialized)."""
    with _conn_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute(sql, params or ())
    return c


def commit():
    """Commit the current transaction (serialized across threads)."""
    with _conn_lock:
        conn = get_conn()
        conn.commit()


def close_cursor(c):
    """Close a cursor if open."""
    if c and not c.closed:
        c.close()


def _dict_rows(cursor):
    """Fetch all rows as list of dicts."""
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _scalar(cursor):
    """Fetch a single scalar value."""
    row = cursor.fetchone()
    return row[0] if row else None


def init_db():
    """Create all tables if they don't exist."""
    c = execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id              SERIAL PRIMARY KEY,
            token_address   TEXT UNIQUE NOT NULL,
            symbol          TEXT,
            alert_mcap      REAL,
            alert_price     REAL,
            alert_time      TIMESTAMP,
            target_2x_mcap  REAL,
            hit_2x          INTEGER DEFAULT 0,
            hit_loss        INTEGER DEFAULT 0,
            hit_time        TIMESTAMP,
            peak_mcap       REAL,
            resolved        INTEGER DEFAULT 0,
            scan_snapshot   TEXT,
            holder_baseline TEXT
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id                SERIAL PRIMARY KEY,
            address           TEXT UNIQUE NOT NULL,
            first_seen        TIMESTAMP,
            total_early_buys  INTEGER DEFAULT 0,
            successful_buys   INTEGER DEFAULT 0,
            total_calls       INTEGER DEFAULT 0,
            is_smart          INTEGER DEFAULT 0
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS wallet_buys (
            id              SERIAL PRIMARY KEY,
            wallet_address  TEXT NOT NULL,
            token_address   TEXT NOT NULL,
            buy_position    INTEGER,
            detected_at     TIMESTAMP,
            UNIQUE(wallet_address, token_address)
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS learning_stats (
            id               SERIAL PRIMARY KEY,
            iteration        INTEGER,
            filter_name      TEXT,
            filter_value     TEXT,
            total_calls      INTEGER,
            successful_calls INTEGER,
            success_rate     REAL,
            recorded_at      TIMESTAMP
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS filter_config (
            id         SERIAL PRIMARY KEY,
            name       TEXT UNIQUE NOT NULL,
            value      TEXT NOT NULL,
            updated_at TIMESTAMP
        )
    """)
    close_cursor(c)

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

    # ── Bot users (Phase 1 wallets) ─────────────────────────────
    c = execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            id              SERIAL PRIMARY KEY,
            telegram_id     BIGINT UNIQUE NOT NULL,
            username        TEXT,
            first_seen      TIMESTAMP,
            last_active     TIMESTAMP,
            is_whitelisted  BOOLEAN DEFAULT FALSE
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            wallet_id       INTEGER NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
            type            TEXT NOT NULL,
            token_address   TEXT NOT NULL,
            token_symbol    TEXT,
            amount_sol      REAL,
            amount_token    REAL,
            price_sol       REAL,
            price_usd       REAL,
            tx_signature    TEXT,
            status          TEXT DEFAULT 'pending',
            slippage_bps    INTEGER DEFAULT 500,
            created_at      TIMESTAMP
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS user_wallets (
            id                    SERIAL PRIMARY KEY,
            user_id               INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            label                 TEXT NOT NULL,
            public_key            TEXT UNIQUE NOT NULL,
            encrypted_private_key TEXT NOT NULL,
            is_default            BOOLEAN DEFAULT FALSE,
            slippage_bps          INTEGER DEFAULT 500,
            created_at            TIMESTAMP,
            last_used             TIMESTAMP,
            UNIQUE(user_id, label)
        )
    """)
    close_cursor(c)

    # ── Auto-trade config ────────────────────────────────────────
    c = execute("""
        CREATE TABLE IF NOT EXISTS auto_trade_config (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            wallet_id       INTEGER NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
            is_enabled      BOOLEAN DEFAULT FALSE,
            buy_amount_sol  REAL DEFAULT 0.1,
            max_positions   INTEGER DEFAULT 3,
            take_profit_pct REAL DEFAULT 100.0,
            stop_loss_pct   REAL DEFAULT 50.0,
            cooldown_minutes INTEGER DEFAULT 60,
            created_at      TIMESTAMP,
            updated_at      TIMESTAMP,
            UNIQUE(user_id, wallet_id)
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            wallet_id       INTEGER NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
            alert_id        INTEGER REFERENCES alerts(id),
            token_address   TEXT NOT NULL,
            token_symbol    TEXT,
            entry_mcap      REAL,
            entry_price_sol REAL,
            amount_sol_invested REAL,
            amount_token    REAL,
            token_decimals  INTEGER DEFAULT 6,
            status          TEXT DEFAULT 'open',
            pnl_pct         REAL,
            exit_reason     TEXT,
            exited_at       TIMESTAMP,
            created_at      TIMESTAMP
        )
    """)
    close_cursor(c)

    # ── Monetization (Phase 4) ────────────────────────────────────
    c = execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            tier            TEXT NOT NULL DEFAULT 'free',
            status          TEXT NOT NULL DEFAULT 'active',
            source          TEXT DEFAULT 'trial',
            starts_at       TIMESTAMP,
            expires_at      TIMESTAMP,
            grace_until     TIMESTAMP,
            warned_48h      BOOLEAN DEFAULT FALSE,
            pro_channel_invite TEXT,
            created_at      TIMESTAMP,
            updated_at      TIMESTAMP,
            UNIQUE(user_id)
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            subscription_id INTEGER REFERENCES subscriptions(id),
            provider        TEXT NOT NULL,
            provider_charge_id TEXT UNIQUE,
            amount          REAL,
            currency        TEXT DEFAULT 'SOL',
            sol_tx_sig      TEXT,
            promo_code      TEXT,
            plan            TEXT,
            status          TEXT DEFAULT 'paid',
            created_at      TIMESTAMP
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id              SERIAL PRIMARY KEY,
            code            TEXT UNIQUE NOT NULL,
            duration_days   INTEGER DEFAULT 7,
            batch_date      DATE,
            max_uses        INTEGER DEFAULT 1,
            used_by         BIGINT,
            used_at         TIMESTAMP,
            expires_at      TIMESTAMP,
            created_at      TIMESTAMP
        )
    """)
    close_cursor(c)

    c = execute("""
        CREATE TABLE IF NOT EXISTS deposit_invoices (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
            plan            TEXT NOT NULL,
            expected_sol    REAL,
            receive_address TEXT UNIQUE NOT NULL,
            encrypted_privkey TEXT NOT NULL,
            status          TEXT DEFAULT 'awaiting',
            created_at      TIMESTAMP,
            expires_at      TIMESTAMP,
            paid_tx_sig     TEXT
        )
    """)
    close_cursor(c)

    commit()
    logger.info("Database initialized (PostgreSQL)")

    # Migrate: add newer columns if missing
    _migrate_add_column("alerts", "hit_loss", "INTEGER DEFAULT 0")
    _migrate_add_column("alerts", "last_milestone", "REAL DEFAULT 0")
    _migrate_add_column("alerts", "wash_score", "INTEGER DEFAULT 0")
    _migrate_add_column("alerts", "creator_address", "TEXT")
    _migrate_add_column("user_wallets", "slippage_bps", "INTEGER DEFAULT 500")
    _migrate_add_column("trades", "price_usd", "REAL")


def _migrate_add_column(table, column, col_type):
    """Safely add a column if it doesn't exist. table/column must be valid identifiers."""
    import re
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table):
        raise ValueError(f"Invalid table name: {table}")
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', column):
        raise ValueError(f"Invalid column name: {column}")
    if not re.match(r'^[a-zA-Z0-9_ ()]+$', col_type):
        raise ValueError(f"Invalid column type: {col_type}")
    try:
        c = execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
        close_cursor(c)
        commit()
    except Exception:
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
            cur.close()
        except Exception:
            pass


# ── Alert helpers ─────────────────────────────────────────────────────

def save_alert(token_address, symbol, alert_mcap, alert_price, target_mcap, snapshot,
               wash_score=0, creator_address=None):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO alerts (token_address, symbol, alert_mcap, alert_price, alert_time,
                            target_2x_mcap, scan_snapshot, wash_score, creator_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (token_address) DO NOTHING
    """, (token_address, symbol, alert_mcap, alert_price, now, target_mcap,
          json.dumps(snapshot), wash_score, creator_address))
    close_cursor(c)
    commit()
    c2 = execute("SELECT id FROM alerts WHERE token_address = %s", (token_address,))
    row_id = _scalar(c2)
    close_cursor(c2)
    return row_id


def get_pending_alerts():
    c = execute("SELECT * FROM alerts WHERE resolved = 0")
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def mark_resolved(token_address, hit_2x, peak_mcap=None, hit_loss=False):
    now = datetime.now(timezone.utc).isoformat()
    if hit_2x:
        c = execute("""
            UPDATE alerts SET hit_2x = 1, hit_time = %s, peak_mcap = %s, resolved = 1
            WHERE token_address = %s AND resolved = 0
        """, (now, peak_mcap, token_address))
    elif hit_loss:
        c = execute("""
            UPDATE alerts SET hit_loss = 1, hit_time = %s, peak_mcap = %s, resolved = 1
            WHERE token_address = %s AND resolved = 0
        """, (now, peak_mcap, token_address))
    else:
        c = execute("""
            UPDATE alerts SET resolved = 1, peak_mcap = %s
            WHERE token_address = %s AND resolved = 0
        """, (peak_mcap, token_address))
    close_cursor(c)
    commit()


def get_stats():
    c = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 1")
    total = _scalar(c) or 0
    close_cursor(c)
    c2 = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 1 AND hit_2x = 1")
    success = _scalar(c2) or 0
    close_cursor(c2)
    rate = (success / total * 100) if total > 0 else 0.0
    return total, success, round(rate, 1)


def get_loss_counts():
    c = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 1 AND hit_loss = 1")
    count = _scalar(c) or 0
    close_cursor(c)
    return count


# ── Wallet helpers (Phase 2) ──────────────────────────────────────────

def save_wallet_buy(wallet_address, token_address, buy_position):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO wallet_buys (wallet_address, token_address, buy_position, detected_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (wallet_address, token_address) DO NOTHING
    """, (wallet_address, token_address, buy_position, now))
    close_cursor(c)
    c2 = execute("""
        INSERT INTO wallets (address, first_seen) VALUES (%s, %s)
        ON CONFLICT (address) DO NOTHING
    """, (wallet_address, now))
    close_cursor(c2)
    c3 = execute("UPDATE wallets SET total_early_buys = total_early_buys + 1 WHERE address = %s",
                 (wallet_address,))
    close_cursor(c3)
    commit()


def get_smart_wallets(min_hits=3, min_rate=0.6):
    c = execute("SELECT * FROM wallets WHERE total_early_buys >= %s AND successful_buys >= %s",
                (min_hits, int(min_hits * min_rate)))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


# ── Learning helpers ──────────────────────────────────────────────────

def save_learning_entry(iteration, filter_name, value, total, success, rate):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO learning_stats (iteration, filter_name, filter_value, total_calls, successful_calls, success_rate, recorded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (iteration, filter_name, value, total, success, rate, now))
    close_cursor(c)
    commit()


def get_filter_config(name):
    c = execute("SELECT value FROM filter_config WHERE name = %s", (name,))
    row = c.fetchone()
    close_cursor(c)
    return row[0] if row else None


def set_filter_config(name, value):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO filter_config (name, value, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
    """, (name, value, now))
    close_cursor(c)
    commit()


def get_all_filter_configs():
    c = execute("SELECT name, value FROM filter_config")
    rows = c.fetchall()
    close_cursor(c)
    return {r[0]: r[1] for r in rows}


# ── Bot users & wallet helpers (Phase 1) ──────────────────────────────

def get_or_create_user(telegram_id, username=None):
    """Get existing user or create a new one. Returns user dict."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute(
        "SELECT id, telegram_id, username, first_seen, last_active, is_whitelisted "
        "FROM bot_users WHERE telegram_id = %s", (telegram_id,))
    user = _dict_rows(c)
    close_cursor(c)
    if user:
        uid = user[0]["id"]
        c2 = execute("UPDATE bot_users SET last_active = %s, username = COALESCE(%s, username) WHERE id = %s",
                     (now, username, uid))
        close_cursor(c2)
        commit()
        user[0]["last_active"] = now
        return user[0]
    c3 = execute("""
        INSERT INTO bot_users (telegram_id, username, first_seen, last_active)
        VALUES (%s, %s, %s, %s)
        RETURNING id, telegram_id, username, first_seen, last_active, is_whitelisted
    """, (telegram_id, username, now, now))
    new_user = _dict_rows(c3)
    close_cursor(c3)
    commit()
    return new_user[0] if new_user else None


def get_user_by_telegram_id(telegram_id):
    c = execute(
        "SELECT id, telegram_id, username, first_seen, last_active, is_whitelisted "
        "FROM bot_users WHERE telegram_id = %s", (telegram_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def create_user_wallet(user_id, label, public_key, encrypted_private_key):
    """Create a new wallet for a user. First wallet is auto-default."""
    now = datetime.now(timezone.utc).isoformat()
    existing = get_user_wallets(user_id)
    is_default = len(existing) == 0
    c = execute("""
        INSERT INTO user_wallets (user_id, label, public_key, encrypted_private_key, is_default, created_at, last_used)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id, user_id, label, public_key, is_default, created_at
    """, (user_id, label, public_key, encrypted_private_key, is_default, now, now))
    wallet = _dict_rows(c)
    close_cursor(c)
    commit()
    return wallet[0] if wallet else None


# Column list used for wallet read queries (omits encrypted_private_key for safety)
_WALLET_READ_COLS = "id, user_id, label, public_key, is_default, slippage_bps, created_at, last_used"
# Full column list including encrypted_private_key (only for export-needed queries)
_WALLET_FULL_COLS = "id, user_id, label, public_key, encrypted_private_key, is_default, slippage_bps, created_at, last_used"


def get_user_wallets(user_id):
    c = execute(f"SELECT {_WALLET_READ_COLS} FROM user_wallets "
                "WHERE user_id = %s ORDER BY created_at ASC", (user_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def get_default_wallet(user_id, include_privkey=False):
    cols = _WALLET_FULL_COLS if include_privkey else _WALLET_READ_COLS
    c = execute(f"SELECT {cols} FROM user_wallets "
                "WHERE user_id = %s AND is_default = TRUE", (user_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    if rows:
        return rows[0]
    wallets = get_user_wallets(user_id)
    if wallets:
        set_default_wallet(user_id, wallets[0]["id"])
        return get_default_wallet(user_id, include_privkey)
    return None


def set_default_wallet(user_id, wallet_id):
    c = execute("UPDATE user_wallets SET is_default = FALSE WHERE user_id = %s", (user_id,))
    close_cursor(c)
    c2 = execute("UPDATE user_wallets SET is_default = TRUE WHERE id = %s AND user_id = %s",
                 (wallet_id, user_id))
    close_cursor(c2)
    commit()


def get_wallet_by_label(user_id, label, include_privkey=False):
    cols = _WALLET_FULL_COLS if include_privkey else _WALLET_READ_COLS
    c = execute(f"SELECT {cols} FROM user_wallets "
                "WHERE user_id = %s AND label = %s", (user_id, label))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


# ── Trade helpers ──────────────────────────────────────────────────────────

_TRADE_COLS = ("id, user_id, wallet_id, type, token_address, token_symbol, "
               "amount_sol, amount_token, price_sol, price_usd, "
               "tx_signature, status, slippage_bps, created_at")


def save_trade(user_id, wallet_id, trade_type, token_address, token_symbol=None,
               amount_sol=None, amount_token=None, price_sol=None, price_usd=None,
               tx_signature=None, slippage_bps=500):
    """Insert a new trade record. Returns the new trade id."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO trades (user_id, wallet_id, type, token_address, token_symbol,
                            amount_sol, amount_token, price_sol, price_usd,
                            tx_signature, slippage_bps, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, wallet_id, trade_type, token_address, token_symbol,
          amount_sol, amount_token, price_sol, price_usd,
          tx_signature, slippage_bps, now))
    trade_id = _scalar(c)
    close_cursor(c)
    commit()
    return trade_id


def get_user_trades(user_id, limit=20):
    """Get the most recent trades for a user."""
    c = execute(f"SELECT {_TRADE_COLS} FROM trades "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def get_trade_by_sig(signature):
    """Look up a trade by its on-chain transaction signature."""
    c = execute(f"SELECT {_TRADE_COLS} FROM trades WHERE tx_signature = %s",
                (signature,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def get_last_buy_trade(user_id, token_address):
    """Most recent recorded buy trade for a user + token (for sell PnL entry)."""
    c = execute(f"SELECT {_TRADE_COLS} FROM trades "
                "WHERE user_id = %s AND token_address = %s "
                "AND type = 'buy' AND price_sol IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, token_address))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def get_alert_for_token(token_address):
    """The bot's alert record for a token (symbol + entry mcap), if any."""
    c = execute("SELECT id, symbol, alert_mcap FROM alerts "
                "WHERE token_address = %s", (token_address,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def update_trade_status(trade_id, status, tx_sig=None):
    """Update the status (and optionally tx_signature) of a trade."""
    if tx_sig is not None:
        c = execute("""
            UPDATE trades SET status = %s, tx_signature = %s
            WHERE id = %s
        """, (status, tx_sig, trade_id))
    else:
        c = execute("UPDATE trades SET status = %s WHERE id = %s",
                    (status, trade_id))
    close_cursor(c)
    commit()


# ── Auto-trade helpers (Phase 3) ──────────────────────────────────────

_AUTO_TRADE_FIELDS = ("is_enabled", "buy_amount_sol", "max_positions",
                      "take_profit_pct", "stop_loss_pct", "cooldown_minutes")


def get_auto_trade_config(user_id, wallet_id=None):
    """Get a user's auto-trade config (for a specific wallet, or any)."""
    if wallet_id is None:
        c = execute("SELECT * FROM auto_trade_config WHERE user_id = %s",
                    (user_id,))
    else:
        c = execute("SELECT * FROM auto_trade_config "
                    "WHERE user_id = %s AND wallet_id = %s",
                    (user_id, wallet_id))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def upsert_auto_trade_config(user_id, wallet_id, updates=None):
    """Create the config row with defaults if missing, then apply updates."""
    now = datetime.now(timezone.utc).isoformat()
    if get_auto_trade_config(user_id, wallet_id) is None:
        c = execute("""
            INSERT INTO auto_trade_config (user_id, wallet_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, wallet_id) DO NOTHING
        """, (user_id, wallet_id, now, now))
        close_cursor(c)
        commit()
    if updates:
        sets, params = [], []
        for key, value in updates.items():
            if key in _AUTO_TRADE_FIELDS:
                sets.append(f"{key} = %s")
                params.append(value)
        if sets:
            sets.append("updated_at = %s")
            params.extend([now, user_id, wallet_id])
            c = execute(
                f"UPDATE auto_trade_config SET {', '.join(sets)} "
                "WHERE user_id = %s AND wallet_id = %s",
                tuple(params))
            close_cursor(c)
            commit()
    return get_auto_trade_config(user_id, wallet_id)


def get_enabled_auto_traders():
    """All enabled auto-trade configs joined with their wallet's public key."""
    c = execute("""
        SELECT cfg.id, cfg.user_id, cfg.wallet_id, cfg.buy_amount_sol,
               cfg.max_positions, cfg.take_profit_pct, cfg.stop_loss_pct,
               cfg.cooldown_minutes, w.public_key, w.label, w.slippage_bps
        FROM auto_trade_config cfg
        JOIN user_wallets w ON w.id = cfg.wallet_id
        WHERE cfg.is_enabled = TRUE
    """)
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def get_wallet_by_id(wallet_id, include_privkey=False):
    cols = _WALLET_FULL_COLS if include_privkey else _WALLET_READ_COLS
    c = execute(f"SELECT {cols} FROM user_wallets WHERE id = %s", (wallet_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def count_open_positions(user_id, wallet_id):
    c = execute("SELECT COUNT(*) FROM positions "
                "WHERE user_id = %s AND wallet_id = %s AND status = 'open'",
                (user_id, wallet_id))
    n = _scalar(c) or 0
    close_cursor(c)
    return n


def get_last_position_time(user_id, wallet_id):
    """Timestamp of the most recent position (open or closed) for cooldowns."""
    c = execute("SELECT MAX(created_at) FROM positions "
                "WHERE user_id = %s AND wallet_id = %s", (user_id, wallet_id))
    last = _scalar(c)
    close_cursor(c)
    return last


def create_position(user_id, wallet_id, alert_id, token_address, token_symbol,
                    entry_mcap, entry_price_sol, amount_sol_invested,
                    amount_token, token_decimals):
    """Record an opened auto-trade position. Returns the position id.

    amount_token is in human-readable units (raw amount / 10**decimals)."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO positions (user_id, wallet_id, alert_id, token_address,
                               token_symbol, entry_mcap, entry_price_sol,
                               amount_sol_invested, amount_token,
                               token_decimals, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
        RETURNING id
    """, (user_id, wallet_id, alert_id, token_address, token_symbol,
          entry_mcap, entry_price_sol, amount_sol_invested, amount_token,
          token_decimals, now))
    pos_id = _scalar(c)
    close_cursor(c)
    commit()
    return pos_id


def get_open_positions(token_address=None):
    if token_address:
        c = execute("SELECT * FROM positions "
                    "WHERE status = 'open' AND token_address = %s",
                    (token_address,))
    else:
        c = execute("SELECT * FROM positions WHERE status = 'open'")
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def has_any_position(user_id, wallet_id, token_address):
    """True if this wallet already has a position (open or closed) on the token."""
    c = execute("SELECT 1 FROM positions "
                "WHERE user_id = %s AND wallet_id = %s AND token_address = %s "
                "LIMIT 1", (user_id, wallet_id, token_address))
    row = c.fetchone()
    close_cursor(c)
    return row is not None


def close_position(position_id, pnl_pct=None, exit_reason=None):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        UPDATE positions
        SET status = 'closed', pnl_pct = %s, exit_reason = %s, exited_at = %s
        WHERE id = %s
    """, (pnl_pct, exit_reason, now, position_id))
    close_cursor(c)
    commit()

# ── Subscriptions & monetization (Phase 4) ────────────────────────────

def get_subscription(user_id):
    """Current subscription row for a user, or None."""
    c = execute("SELECT * FROM subscriptions WHERE user_id = %s", (user_id,))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else None


def ensure_subscription(user_id):
    """Guarantee a subscription row exists (trial tier, no expiry yet)."""
    if get_subscription(user_id) is None:
        now = datetime.now(timezone.utc).isoformat()
        c = execute("""
            INSERT INTO subscriptions (user_id, tier, status, source, starts_at,
                                       created_at, updated_at)
            VALUES (%s, 'trial', 'active', 'trial', %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id, now, now, now))
        close_cursor(c)
        commit()
    return get_subscription(user_id)


def activate_subscription(user_id, days, source, plan=None, promo_code=None,
                          payment_id=None):
    """Grant/extend Pro access. Trial converts to Pro starting now (or from
    an existing expiry, so paid time never gets eaten by remaining trial)."""
    from config import GRACE_HOURS
    now = datetime.now(timezone.utc)
    now_s = now.isoformat()
    ensure_subscription(user_id)
    sub = get_subscription(user_id)
    base = sub.get("expires_at") or now
    if isinstance(base, str):
        base = datetime.fromisoformat(base)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    # paid time extends from max(now, current expiry) but never less than now
    start = max(base, now)
    expires = start + timedelta(days=days)
    grace = expires + timedelta(hours=GRACE_HOURS)
    c = execute("""
        UPDATE subscriptions
        SET tier = 'pro', status = 'active', source = %s,
            starts_at = %s, expires_at = %s, grace_until = %s,
            warned_48h = FALSE, updated_at = %s
        WHERE user_id = %s
    """, (source, now_s, expires.isoformat(), grace.isoformat(), now_s, user_id))
    close_cursor(c)
    commit()
    if payment_id:
        c = execute("UPDATE payments SET subscription_id = (SELECT id FROM subscriptions WHERE user_id = %s) WHERE id = %s",
                    (user_id, payment_id))
        close_cursor(c)
        commit()
    logger.info("Subscription %s: +%.0fd for user %s (until %s)",
                source, days, user_id, expires.isoformat())
    return get_subscription(user_id)


def _parse_utc(dtval):
    if isinstance(dtval, datetime):
        dt = dtval
    else:
        dt = datetime.fromisoformat(str(dtval))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_pro(user_id, include_grace=False):
    """True while tier is pro (or trial) and not past expiry (+ optional grace)."""
    sub = get_subscription(user_id)
    if not sub or sub.get("tier") not in ("pro", "trial"):
        return False
    if sub.get("status") == "expired":
        return False
    expires = sub.get("expires_at")
    if not expires:
        return sub.get("status") == "active"
    expiry = _parse_utc(expires)
    if include_grace and sub.get("grace_until"):
        expiry = _parse_utc(sub["grace_until"])
    return datetime.now(timezone.utc) <= expiry


def is_paid_pro(user_id):
    """True only for paid Pro (excludes trial tier)."""
    sub = get_subscription(user_id)
    return bool(sub) and sub.get("tier") == "pro" and is_pro(user_id)


def get_expired_in_grace():
    """Active subs whose expiry passed — flip to grace, return user_ids."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        SELECT id, user_id, grace_until FROM subscriptions
        WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < %s
    """, (now,))
    rows = _dict_rows(c)
    close_cursor(c)
    out = []
    for r in rows:
        c = execute("UPDATE subscriptions SET status = 'grace', updated_at = %s WHERE id = %s",
                    (now, r["id"]))
        close_cursor(c)
        out.append(r)
    commit()
    return out


def get_grace_expired():
    """Grace subs past grace_until — flip to expired, return user_ids."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        SELECT id, user_id FROM subscriptions
        WHERE status = 'grace' AND grace_until IS NOT NULL AND grace_until < %s
    """, (now,))
    rows = _dict_rows(c)
    close_cursor(c)
    out = []
    for r in rows:
        c = execute("""
            UPDATE subscriptions
            SET status = 'expired', tier = 'free', updated_at = %s
            WHERE id = %s
        """, (now, r["id"]))
        close_cursor(c)
        out.append(r)
    commit()
    return out


def get_untouched_trial_expiry():
    """Subs still on trial tier — used by the expiry warning job."""
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        SELECT id, user_id, expires_at FROM subscriptions
        WHERE tier = 'trial' AND status = 'active'
          AND expires_at IS NOT NULL AND expires_at < %s
    """, (now,))
    rows = _dict_rows(c)
    close_cursor(c)
    for r in rows:
        c = execute("""
            UPDATE subscriptions
            SET status = 'expired', tier = 'free', updated_at = %s
            WHERE id = %s
        """, (now, r["id"]))
        close_cursor(c)
    commit()
    return rows


def set_48h_warned(sub_id):
    c = execute("UPDATE subscriptions SET warned_48h = TRUE WHERE id = %s", (sub_id,))
    close_cursor(c)
    commit()


def get_telegram_ids_by_tier(tiers, statuses=("active",)):
    """telegram_ids of users whose subscription tier is in `tiers`."""
    if not tiers:
        return []
    c = execute("""
        SELECT u.telegram_id FROM subscriptions s
        JOIN bot_users u ON u.id = s.user_id
        WHERE s.tier = ANY(%s) AND s.status = ANY(%s)
    """, (list(tiers), list(statuses)))
    rows = _dict_rows(c)
    close_cursor(c)
    return [r["telegram_id"] for r in rows]


def get_telegram_id(user_id):
    c = execute("SELECT telegram_id FROM bot_users WHERE id = %s", (user_id,))
    tid = _scalar(c)
    close_cursor(c)
    return tid


def get_user_id_by_telegram(telegram_id):
    c = execute("SELECT id FROM bot_users WHERE telegram_id = %s", (telegram_id,))
    uid = _scalar(c)
    close_cursor(c)
    return uid


# ── Payments ──────────────────────────────────────────────────────────

def record_payment(user_id, provider, amount, currency="SOL", plan=None,
                   provider_charge_id=None, sol_tx_sig=None, promo_code=None,
                   status="paid"):
    """Insert a payment row; idempotent on provider_charge_id. Returns id."""
    now = datetime.now(timezone.utc).isoformat()
    if provider_charge_id:
        c = execute("SELECT id FROM payments WHERE provider_charge_id = %s",
                    (provider_charge_id,))
        existing = _scalar(c)
        close_cursor(c)
        if existing:
            return existing
    c = execute("""
        INSERT INTO payments (user_id, provider, provider_charge_id, amount,
                              currency, sol_tx_sig, promo_code, plan, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, provider, provider_charge_id, amount, currency,
          sol_tx_sig, promo_code, plan, status, now))
    pid = _scalar(c)
    close_cursor(c)
    commit()
    return pid


# ── Promo codes ───────────────────────────────────────────────────────

_PROMO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def create_promo_batch(count, duration_days, batch_date):
    """Generate `count` single-use codes for a day. Returns the code list."""
    import secrets
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    # codes die at 23:59:59 UTC on the batch date
    expires = datetime(now.year, now.month, now.day, 23, 59, 59,
                       tzinfo=timezone.utc)
    if now >= expires:  # safety: generation after expiry window
        expires = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59)
    codes = []
    for _ in range(count):
        code = "GEM-" + "".join(secrets.choice(_PROMO_ALPHABET)
                                for _ in range(10))
        c = execute("""
            INSERT INTO promo_codes (code, duration_days, batch_date, max_uses,
                                     expires_at, created_at)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON CONFLICT (code) DO NOTHING
        """, (code, duration_days, batch_date, expires.isoformat(), now.isoformat()))
        close_cursor(c)
        codes.append(code)
    commit()
    return codes


def redeem_promo(code, telegram_id):
    """Atomically claim a code for this user. Returns (ok, message, days)."""
    from config import PROMO_DURATION_DAYS
    now = datetime.now(timezone.utc)
    if get_user_id_by_telegram(telegram_id) is None:
        return False, "Send /start first.", 0
    # rate limit: max 3 failed attempts/day handled by caller; here atomic claim
    c = execute("""
        UPDATE promo_codes
        SET used_by = %s, used_at = %s
        WHERE code = %s AND used_by IS NULL AND expires_at > %s
        RETURNING id, duration_days
    """, (telegram_id, now.isoformat(), code.strip().upper(), now.isoformat()))
    row = c.fetchone()
    close_cursor(c)
    commit()
    if not row:
        # diagnose why
        c = execute("SELECT used_by, expires_at FROM promo_codes WHERE code = %s",
                    (code.strip().upper(),))
        info = c.fetchone()
        close_cursor(c)
        if not info:
            return False, "That code doesn't exist. Check @thomas_young — codes drop daily.", 0
        used_by, expires_at = info
        if used_by is not None:
            return False, "Too late — someone already claimed that code. ⏱️", 0
        return False, "That code expired (codes die same day). Watch for the next drop.", 0
    promo_id, days = row[0], row[1] or PROMO_DURATION_DAYS
    activate_subscription(get_user_id_by_telegram(telegram_id), days,
                          source="promo", plan="week", promo_code=code)
    c = execute("""
        UPDATE payments SET promo_code = %s, provider = 'promo', currency = 'PROMO'
        WHERE user_id = %s AND promo_code = %s AND provider = 'promo'
    """, (code, get_user_id_by_telegram(telegram_id), code))
    close_cursor(c)
    record_payment(get_user_id_by_telegram(telegram_id), "promo", 0,
                   currency="PROMO", plan="week", promo_code=code)
    return True, f"Pro unlocked for {days} days! 🤖", days


def get_today_promo_stats(batch_date):
    c = execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE used_by IS NOT NULL) AS used,
               COUNT(*) FILTER (WHERE used_by IS NULL AND expires_at > %s) AS live
        FROM promo_codes WHERE batch_date = %s
    """, (datetime.now(timezone.utc).isoformat(), batch_date))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows[0] if rows else {"total": 0, "used": 0, "live": 0}


# ── Deposit invoices (SOL rail) ───────────────────────────────────────

def create_deposit_invoice(user_id, plan, expected_sol, receive_address,
                           encrypted_privkey, ttl_minutes):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    # one active invoice per user: supersede older ones
    c = execute("""
        UPDATE deposit_invoices SET status = 'expired'
        WHERE user_id = %s AND status = 'awaiting'
    """, (user_id,))
    close_cursor(c)
    c = execute("""
        INSERT INTO deposit_invoices (user_id, plan, expected_sol, receive_address,
                                      encrypted_privkey, status, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, 'awaiting', %s, %s)
        RETURNING id
    """, (user_id, plan, expected_sol, receive_address, encrypted_privkey,
          now.isoformat(),
          (now + timedelta(minutes=ttl_minutes)).isoformat()))
    inv_id = _scalar(c)
    close_cursor(c)
    commit()
    return inv_id


def get_pending_invoices():
    c = execute("""
        SELECT * FROM deposit_invoices
        WHERE status = 'awaiting' AND expires_at > %s
    """, (datetime.now(timezone.utc).isoformat(),))
    rows = _dict_rows(c)
    close_cursor(c)
    return rows


def mark_invoice_paid(invoice_id, tx_sig):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        UPDATE deposit_invoices
        SET status = 'paid', paid_tx_sig = %s
        WHERE id = %s AND status = 'awaiting'
        RETURNING user_id, plan
    """, (tx_sig, invoice_id))
    row = c.fetchone()
    close_cursor(c)
    commit()
    return row  # (user_id, plan) or None


def expire_stale_invoices():
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        UPDATE deposit_invoices SET status = 'expired'
        WHERE status = 'awaiting' AND expires_at <= %s
        RETURNING user_id
    """, (now,))
    rows = _dict_rows(c)
    close_cursor(c)
    commit()
    return rows


def kv_get(key):
    """Tiny KV store (reuses filter_config) for job state like last run dates."""
    c = execute("SELECT value FROM filter_config WHERE name = %s", (key,))
    row = c.fetchone()
    close_cursor(c)
    return row[0] if row else None


def kv_set(key, value):
    set_filter_config(key, str(value))
