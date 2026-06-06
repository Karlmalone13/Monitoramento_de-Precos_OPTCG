"""
sync_all.py — Sincroniza TODAS as cartas One Piece do CardTrader.
Usa /marketplace/products?expansion_id=X para buscar todos os preços
de uma expansão de uma vez (muito mais rápido que carta por carta).
"""

import os
import json
import logging
import sqlite3
import urllib.request
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

CARDTRADER_TOKEN = os.environ.get("CARDTRADER_TOKEN", "")
CT_BASE = "https://api.cardtrader.com/api/v2"
OP_GAME_ID = 15
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "monitor.db")

logger = logging.getLogger(__name__)

SKIP_CODES = {"st31", "st32", "st33", "st34", "st35", "st36", "eb-05"}

sync_progress = {
    "current": 0,
    "total": 0,
    "running": False,
    "current_expansion": ""
}


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_sync_tables():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS all_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blueprint_id INTEGER UNIQUE NOT NULL,
            collector_number TEXT,
            name TEXT,
            expansion_code TEXT,
            expansion_name TEXT,
            image_url TEXT,
            rarity TEXT,
            last_price REAL,
            last_price_date TEXT,
            prev_price REAL,
            variation_pct REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS all_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blueprint_id INTEGER NOT NULL,
            price REAL,
            fetched_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blueprint_id INTEGER NOT NULL,
            collector_number TEXT,
            card_name TEXT,
            expansion TEXT,
            direction TEXT,
            old_price REAL,
            new_price REAL,
            variation_pct REAL,
            image_url TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def ct_get(path: str):
    if not CARDTRADER_TOKEN:
        return None
    url = f"{CT_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {CARDTRADER_TOKEN}",
                 "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, dict) and "array" in data:
                return data["array"]
            return data
    except Exception as e:
        logger.warning(f"[CT API] {path}: {e}")
        return None


def get_threshold():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'threshold_pct'")
    row = c.fetchone()
    conn.close()
    return float(row["value"]) if row else 20.0


def save_auto_alert(blueprint_id, collector, name, expansion, direction,
                    old_price, new_price, variation_pct, image_url):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO auto_alerts
        (blueprint_id, collector_number, card_name, expansion, direction,
         old_price, new_price, variation_pct, image_url, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (blueprint_id, collector, name, expansion, direction,
          old_price, new_price, round(variation_pct, 2), image_url,
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def sync_expansion(exp: dict, threshold: float) -> int:
    """
    Sincroniza uma expansão completa usando apenas 2 chamadas à API:
    1. GET /blueprints/export?expansion_id=X  → metadados das cartas
    2. GET /marketplace/products?expansion_id=X&language=en → todos os preços
    """
    exp_code = exp.get("code", "")
    exp_name = exp.get("name", "")
    exp_id   = exp["id"]

    if exp_code in SKIP_CODES:
        return 0

    # 1. Busca blueprints (metadados: nome, imagem, collector number, raridade)
    blueprints = ct_get(f"/blueprints/export?expansion_id={exp_id}")
    if not blueprints:
        return 0

    # Indexa blueprints por id para lookup rápido
    bp_map = {}
    for bp in blueprints:
        bp_id = bp["id"]
        image_url = bp.get("image_url", "")
        if image_url and not image_url.startswith("http"):
            image_url = "https://cardtrader.com" + image_url
        bp_map[bp_id] = {
            "name":      bp.get("name", "").replace(".", " ").strip(),
            "collector": bp.get("fixed_properties", {}).get("collector_number", ""),
            "rarity":    bp.get("fixed_properties", {}).get("onepiece_rarity", ""),
            "image_url": image_url,
        }

    # 2a. Busca preços EN primeiro
    def extract_cheapest(products_data):
        result = {}
        if not products_data or not isinstance(products_data, dict):
            return result
        for bp_id_str, products in products_data.items():
            if not isinstance(products, list):
                continue
            available = [
                p for p in products
                if not p.get("on_vacation") and p.get("price", {}).get("cents")
            ]
            if available:
                cheapest = min(available, key=lambda p: p["price"]["cents"])
                try:
                    result[int(bp_id_str)] = round(cheapest["price"]["cents"] / 100, 2)
                except Exception:
                    pass
        return result

    en_data = ct_get(f"/marketplace/products?expansion_id={exp_id}&language=en")
    cheapest_by_bp = extract_cheapest(en_data)

    # 2b. Busca sem filtro de idioma para cartas que nao tem EN disponivel
    all_data = ct_get(f"/marketplace/products?expansion_id={exp_id}")
    all_prices = extract_cheapest(all_data)

    # Combina: EN tem prioridade, fallback para qualquer idioma
    for bp_id, price in all_prices.items():
        if bp_id not in cheapest_by_bp:
            cheapest_by_bp[bp_id] = price

    if not cheapest_by_bp:
        return 0

    # 3. Salva no banco
    conn = get_conn()
    c = conn.cursor()
    processed = 0
    now = datetime.utcnow().isoformat()

    for bp_id, new_price in cheapest_by_bp.items():
        bp = bp_map.get(bp_id)
        if not bp:
            continue

        bp_name   = bp["name"]
        collector = bp["collector"]
        rarity    = bp["rarity"]
        image_url = bp["image_url"]

        # Salva histórico
        c.execute(
            "INSERT INTO all_prices (blueprint_id, price, fetched_at) VALUES (?,?,?)",
            (bp_id, new_price, now)
        )

        # Verifica existente
        c.execute("SELECT last_price FROM all_cards WHERE blueprint_id = ?", (bp_id,))
        existing = c.fetchone()

        if existing:
            old_price = existing["last_price"]
            variation = round(((new_price - old_price) / old_price * 100), 2) if old_price else 0
            c.execute("""
                UPDATE all_cards SET
                    last_price = ?, last_price_date = ?,
                    prev_price = ?, variation_pct = ?,
                    image_url = COALESCE(NULLIF(image_url,''), ?),
                    rarity = COALESCE(NULLIF(rarity,''), ?)
                WHERE blueprint_id = ?
            """, (new_price, now, old_price, variation, image_url, rarity, bp_id))

            if old_price and old_price > 0 and abs(variation) >= threshold:
                direction = "up" if variation > 0 else "down"
                save_auto_alert(
                    bp_id, collector, bp_name, exp_name,
                    direction, old_price, new_price, variation, image_url
                )
                logger.warning(
                    f"[ALERTA] {collector} {bp_name}: "
                    f"€{old_price} → €{new_price} ({variation:+.1f}%)"
                )
        else:
            c.execute("""
                INSERT OR IGNORE INTO all_cards
                (blueprint_id, collector_number, name, expansion_code,
                 expansion_name, image_url, rarity, last_price, last_price_date,
                 prev_price, variation_pct)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (bp_id, collector, bp_name, exp_code, exp_name,
                  image_url, rarity, new_price, now, None, 0))

        processed += 1

    conn.commit()
    conn.close()
    return processed


def run_full_sync():
    import scheduler as sched
    if hasattr(sched, '_sync_running') and sched._sync_running:
        logger.warning("[Sync] Já existe uma sincronização em andamento, abortando.")
        return

    sched._sync_running = True
    sync_progress["running"] = True
    sync_progress["current"] = 0
    sync_progress["total"] = 0
    sync_progress["current_expansion"] = ""

    try:
        if not CARDTRADER_TOKEN:
            logger.error("[Sync] Token CardTrader não configurado!")
            return

        init_sync_tables()
        threshold = get_threshold()
        logger.info(f"[Sync] Iniciando sincronização otimizada (threshold={threshold}%)...")
        start = datetime.utcnow()

        expansions = ct_get("/expansions")
        if not expansions:
            logger.error("[Sync] Não foi possível buscar expansões.")
            return

        op_expansions = [e for e in expansions if e.get("game_id") == OP_GAME_ID]
        sync_progress["total"] = len(op_expansions)
        logger.info(f"[Sync] {len(op_expansions)} expansões para processar.")

        total = 0
        for i, exp in enumerate(op_expansions):
            code = exp.get("code", "—")
            name = exp.get("name", "")
            sync_progress["current"] = i + 1
            sync_progress["current_expansion"] = name
            logger.info(f"[Sync] [{i+1}/{len(op_expansions)}] {code} — {name}")
            count = sync_expansion(exp, threshold)
            total += count
            logger.info(f"[Sync]   → {count} cartas")

        elapsed = (datetime.utcnow() - start).seconds
        logger.info(f"[Sync] Concluído! {total} cartas em {elapsed}s ({elapsed//60}min {elapsed%60}s).")

        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_full_sync', ?)",
            (datetime.utcnow().isoformat(),)
        )
        conn.commit()
        conn.close()

    finally:
        sched._sync_running = False
        sync_progress["running"] = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    run_full_sync()