"""
Database layer — PostgreSQL via psycopg2.
Uses DATABASE_URL from config (set via Railway Postgres addon).
"""
import json
import logging
from datetime import datetime, timezone

import psycopg2
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_conn = None


def get_conn():
    """Get a singleton connection to PostgreSQL (lazy init)."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        _conn.autocommit = False
    return _conn


def execute(sql, params=None):
    """Convenience: get cursor, execute SQL, return cursor.
    Caller must close the cursor and commit on write operations."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(sql, params or ())
    return c


def commit():
    """Commit the current transaction."""
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

    commit()
    logger.info("Database initialized (PostgreSQL)")

    # Migrate: add newer columns if missing
    _migrate_add_column("alerts", "hit_loss", "INTEGER DEFAULT 0")
    _migrate_add_column("alerts", "last_milestone", "REAL DEFAULT 0")
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

def save_alert(token_address, symbol, alert_mcap, alert_price, target_mcap, snapshot):
    now = datetime.now(timezone.utc).isoformat()
    c = execute("""
        INSERT INTO alerts (token_address, symbol, alert_mcap, alert_price, alert_time,
                            target_2x_mcap, scan_snapshot)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (token_address) DO NOTHING
    """, (token_address, symbol, alert_mcap, alert_price, now, target_mcap, json.dumps(snapshot)))
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