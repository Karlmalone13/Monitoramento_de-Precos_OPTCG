import sqlite3
import os
from datetime import datetime

DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "monitor.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            card_code TEXT,
            image_url TEXT,
            added_at TEXT NOT NULL
        )
    """)
    # Migração: adiciona image_url se não existir
    try:
        c.execute("ALTER TABLE cards ADD COLUMN image_url TEXT")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            price REAL,
            currency TEXT DEFAULT 'EUR',
            url TEXT,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY (card_id) REFERENCES cards(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            direction TEXT NOT NULL,
            old_price REAL,
            new_price REAL,
            variation_pct REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (card_id) REFERENCES cards(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    #---- Adiona migração para card_display_name e expansion_name ----
    try:
        c.execute("ALTER TABLE cards ADD COLUMN card_display_name TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE cards ADD COLUMN expansion_name TEXT")
    except Exception:
        pass

    # default threshold
    c.execute("INSERT OR IGNORE INTO settings VALUES ('threshold_pct', '20')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('check_interval_min', '30')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('email_to', '')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('email_from', '')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('smtp_host', 'smtp.gmail.com')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('smtp_port', '587')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('smtp_password', '')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('telegram_token', '')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('telegram_chat_id', '')")

    conn.commit()
    conn.close()


def add_card(name: str, card_code: str = "", image_url: str = ""):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO cards (name, card_code, image_url, added_at) VALUES (?, ?, ?, ?)",
        (name.strip(), card_code.strip(), image_url, datetime.utcnow().isoformat())
    )
    card_id = c.lastrowid
    conn.commit()
    conn.close()
    return card_id


def update_card_image(card_id: int, image_url: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE cards SET image_url = ? WHERE id = ?", (image_url, card_id))
    conn.commit()
    conn.close()
def update_card_display_name(card_id: int, display_name: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE cards SET card_display_name = ? WHERE id = ?", (display_name, card_id))
    conn.commit()
    conn.close()

def update_card_expansion(card_id: int, expansion_name: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE cards SET expansion_name = ? WHERE id = ?", (expansion_name, card_id))
    conn.commit()
    conn.close()

def remove_card(card_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM prices WHERE card_id = ?", (card_id,))
    c.execute("DELETE FROM alerts WHERE card_id = ?", (card_id,))
    c.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()


def get_all_cards():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM cards ORDER BY added_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def save_price(card_id: int, source: str, price: float, currency: str, url: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO prices (card_id, source, price, currency, url, fetched_at) VALUES (?,?,?,?,?,?)",
        (card_id, source, price, currency, url, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_latest_prices(card_id: int):
    """Returns the most recent price per source for a card."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT source, price, currency, url, fetched_at
        FROM prices
        WHERE card_id = ?
        GROUP BY source
        HAVING fetched_at = MAX(fetched_at)
        ORDER BY source
    """, (card_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_previous_price(card_id: int, source: str):
    """Returns the second-to-last price for a card/source pair."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT price FROM prices
        WHERE card_id = ? AND source = ? AND price IS NOT NULL
        ORDER BY fetched_at DESC
        LIMIT 2
    """, (card_id, source))
    rows = c.fetchall()
    conn.close()
    if len(rows) >= 2:
        return rows[1]["price"]
    return None


def get_price_history(card_id: int, source: str = None, limit: int = 50):
    conn = get_conn()
    c = conn.cursor()
    if source:
        c.execute("""
            SELECT source, price, currency, fetched_at FROM prices
            WHERE card_id = ? AND source = ?
            ORDER BY fetched_at DESC LIMIT ?
        """, (card_id, source, limit))
    else:
        c.execute("""
            SELECT source, price, currency, fetched_at FROM prices
            WHERE card_id = ?
            ORDER BY fetched_at DESC LIMIT ?
        """, (card_id, limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def save_alert(card_id: int, source: str, direction: str,
               old_price: float, new_price: float, variation_pct: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO alerts (card_id, source, direction, old_price, new_price, variation_pct, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (card_id, source, direction, old_price, new_price, variation_pct,
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_alerts(limit: int = 50):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT a.*, ca.name as card_name, ca.card_code
        FROM alerts a
        JOIN cards ca ON ca.id = a.card_id
        ORDER BY a.created_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_setting(key: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    rows = {r["key"]: r["value"] for r in c.fetchall()}
    conn.close()
    return rows