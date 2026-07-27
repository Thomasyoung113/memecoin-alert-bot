import sqlite3
import json
import os
from datetime import datetime, timezone
from config import DB_PATH, DB_DIR


def get_conn() -> sqlite3.Connection:
    """Get a connection to the SQLite DB (simple, not pooled)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_conn()
    c = conn.cursor()

    # Tokens that were alerted
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address   TEXT UNIQUE NOT NULL,
            symbol          TEXT,
            alert_mcap      REAL,
            alert_price     REAL,
            alert_time      TIMESTAMP,
            target_2x_mcap  REAL,
            hit_2x          INTEGER DEFAULT 0,
            hit_time        TIMESTAMP,
            peak_mcap       REAL,
            resolved        INTEGER DEFAULT 0,
            scan_snapshot   TEXT,
            holder_baseline TEXT
        )
    """)

    # Smart wallets (Phase 2)
    c.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            address           TEXT UNIQUE NOT NULL,
            first_seen        TIMESTAMP,
            total_early_buys  INTEGER DEFAULT 0,
            successful_buys   INTEGER DEFAULT 0,
            total_calls       INTEGER DEFAULT 0,
            is_smart          INTEGER DEFAULT 0
        )
    """)

    # Wallet → token buys (Phase 2)
    c.execute("""
        CREATE TABLE IF NOT EXISTS wallet_buys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address  TEXT NOT NULL,
            token_address   TEXT NOT NULL,
            buy_position    INTEGER,
            detected_at     TIMESTAMP,
            FOREIGN KEY (wallet_address) REFERENCES wallets(address),
            FOREIGN KEY (token_address) REFERENCES alerts(token_address)
        )
    """)

    # Learning / filter-tuning history
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration       INTEGER,
            filter_name     TEXT,
            filter_value    TEXT,
            total_calls     INTEGER,
            successful_calls INTEGER,
            success_rate    REAL,
            recorded_at     TIMESTAMP
        )
    """)

    # Current active filter config (the learner writes here)
    c.execute("""
        CREATE TABLE IF NOT EXISTS filter_config (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT UNIQUE NOT NULL,
            value   TEXT NOT NULL,
            updated_at TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    # Migrate existing databases that may lack newer columns
    try:
        conn2 = get_conn()
        conn2.execute("ALTER TABLE alerts ADD COLUMN holder_baseline TEXT")
        conn2.commit()
        conn2.close()
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Alert helpers ─────────────────────────────────────────────────────

def save_alert(token_address: str, symbol: str, alert_mcap: float,
               alert_price: float, target_mcap: float, snapshot: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO alerts
            (token_address, symbol, alert_mcap, alert_price, alert_time,
             target_2x_mcap, scan_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (token_address, symbol, alert_mcap, alert_price, now,
          target_mcap, json.dumps(snapshot)))
    conn.commit()
    row_id = conn.execute(
        "SELECT id FROM alerts WHERE token_address = ?", (token_address,)
    ).fetchone()
    conn.close()
    return row_id["id"] if row_id else None


def get_pending_alerts():
    """Return alerts that haven't resolved yet."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM alerts WHERE resolved = 0"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_resolved(token_address: str, hit_2x: bool, peak_mcap: float = None):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    if hit_2x:
        conn.execute("""
            UPDATE alerts SET hit_2x = 1, hit_time = ?, peak_mcap = ?,
                              resolved = 1
            WHERE token_address = ? AND resolved = 0
        """, (now, peak_mcap, token_address))
    else:
        conn.execute("""
            UPDATE alerts SET resolved = 1, peak_mcap = ?
            WHERE token_address = ? AND resolved = 0
        """, (peak_mcap, token_address))
    conn.commit()
    conn.close()


def get_stats():
    """Return (total_calls, successful_calls, success_rate)."""
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 1"
    ).fetchone()[0]
    success = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 1 AND hit_2x = 1"
    ).fetchone()[0]
    conn.close()
    rate = (success / total * 100) if total > 0 else 0.0
    return total, success, round(rate, 1)


# ── Wallet helpers (Phase 2) ──────────────────────────────────────────

def save_wallet_buy(wallet_address: str, token_address: str,
                    buy_position: int):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO wallet_buys
            (wallet_address, token_address, buy_position, detected_at)
        VALUES (?, ?, ?, ?)
    """, (wallet_address, token_address, buy_position, now))
    conn.execute("""
        INSERT OR IGNORE INTO wallets (address, first_seen)
        VALUES (?, ?)
    """, (wallet_address, now))
    conn.execute("""
        UPDATE wallets SET total_early_buys = total_early_buys + 1
        WHERE address = ?
    """, (wallet_address,))
    conn.commit()
    conn.close()


def get_smart_wallets(min_hits: int = 3, min_rate: float = 0.6):
    """Return wallets that qualify as 'smart money'."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM wallets
        WHERE total_early_buys >= ? AND successful_buys >= ?
    """, (min_hits, int(min_hits * min_rate))).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Learning helpers ──────────────────────────────────────────────────

def save_learning_entry(iteration: int, filter_name: str, value: str,
                        total: int, success: int, rate: float):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO learning_stats
            (iteration, filter_name, filter_value,
             total_calls, successful_calls, success_rate, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (iteration, filter_name, value, total, success, rate, now))
    conn.commit()
    conn.close()


def get_filter_config(name: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM filter_config WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def set_filter_config(name: str, value: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO filter_config (name, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value,
                                        updated_at = excluded.updated_at
    """, (name, value, now))
    conn.commit()
    conn.close()


def get_all_filter_configs() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT name, value FROM filter_config").fetchall()
    conn.close()
    return {r["name"]: r["value"] for r in rows}