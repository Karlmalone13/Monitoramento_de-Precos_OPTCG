"""
app.py — Flask REST API for the One Piece TCG Price Monitor.
"""

import logging
import os
import threading
from flask import Flask, jsonify, request, send_from_directory

import database as db
import scrapers
import alerts as alert_engine
import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__, static_folder="frontend", static_url_path="")


# ─── Serve frontend ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


# ─── Cards ────────────────────────────────────────────────────────────────────

@app.route("/api/cards", methods=["GET"])
def get_cards():
    cards = db.get_all_cards()
    enriched = []
    for card in cards:
        latest = db.get_latest_prices(card["id"])
        enriched.append({**card, "latest_prices": latest})
    return jsonify(enriched)


@app.route("/api/cards", methods=["POST"])
def add_card():
    data = request.json or {}
    name = data.get("name", "").strip()
    code = data.get("card_code", "").strip()
    if not name and not code:
        return jsonify({"error": "Informe o nome ou código da carta"}), 400

    label = name or code
    card_id = db.add_card(label, code)

    # Trigger immediate scrape in background
    def _scrape():
        try:
            results = scrapers.scrape_all(label)
            for source, data in results.items():
                price    = data.get("price")
                currency = data.get("currency", "EUR")
                url      = data.get("url", "")
                if price is not None:
                    db.save_price(card_id, source, price, currency, url)
                # Salva imagem se vier do cardtrader
                if source == "cardtrader" and data.get("image_url"):
                    db.update_card_image(card_id, data["image_url"])
        except Exception as e:
            logging.error(f"Scrape imediato falhou: {e}")

    threading.Thread(target=_scrape, daemon=True).start()

    return jsonify({"id": card_id, "name": label, "status": "scraping_started"}), 201


@app.route("/api/cards/<int:card_id>", methods=["DELETE"])
def delete_card(card_id):
    db.remove_card(card_id)
    return jsonify({"ok": True})


# ─── Prices ───────────────────────────────────────────────────────────────────

@app.route("/api/cards/<int:card_id>/prices", methods=["GET"])
def get_prices(card_id):
    latest  = db.get_latest_prices(card_id)
    history = db.get_price_history(card_id, limit=100)
    return jsonify({"latest": latest, "history": history})


@app.route("/api/cards/<int:card_id>/refresh", methods=["POST"])
def refresh_card(card_id):
    """Force an immediate re-scrape for one card."""
    cards = [c for c in db.get_all_cards() if c["id"] == card_id]
    if not cards:
        return jsonify({"error": "Carta não encontrada"}), 404

    card   = cards[0]
    label  = card["name"]

    def _scrape():
        try:
            results = scrapers.scrape_all(label)
            for source, data in results.items():
                price    = data.get("price")
                currency = data.get("currency", "EUR")
                url      = data.get("url", "")
                if price is not None:
                    db.save_price(card_id, source, price, currency, url)
                    alert_engine.check_and_alert(card_id, label, source, price, currency)
                if source == "cardtrader" and data.get("image_url"):
                    db.update_card_image(card_id, data["image_url"])
        except Exception as e:
            logging.error(f"Refresh falhou para card {card_id}: {e}")

    threading.Thread(target=_scrape, daemon=True).start()
    return jsonify({"ok": True, "status": "scraping_started"})


@app.route("/api/refresh-all", methods=["POST"])
def refresh_all():
    scheduler.run_now()
    return jsonify({"ok": True, "status": "cycle_started"})


# ─── Alerts ───────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_alerts(limit))


# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = db.get_all_settings()
    # Never return the SMTP password
    settings.pop("smtp_password", None)
    return jsonify(settings)


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    data = request.json or {}
    allowed = {
        "threshold_pct", "check_interval_min",
        "email_to", "email_from",
        "smtp_host", "smtp_port", "smtp_password",
        "telegram_token", "telegram_chat_id",
    }
    for key, value in data.items():
        if key in allowed:
            db.set_setting(key, str(value))

    # Restart scheduler so new interval takes effect
    scheduler.stop()
    scheduler.start()

    return jsonify({"ok": True})



# ─── Auto Alerts (sync_all) ───────────────────────────────────────────────────

@app.route("/api/auto-alerts", methods=["GET"])
def get_auto_alerts():
    limit = int(request.args.get("limit", 100))
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT * FROM auto_alerts ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return jsonify(rows)


@app.route("/api/sync-status", methods=["GET"])
def sync_status():
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) as total FROM all_cards")
        total = c.fetchone()["total"]
    except Exception:
        total = 0
    c.execute("SELECT value FROM settings WHERE key = \'last_full_sync\'")
    row = c.fetchone()
    last_sync = row["value"] if row else None
    try:
        c.execute("SELECT COUNT(*) as total FROM auto_alerts")
        total_alerts = c.fetchone()["total"]
    except Exception:
        total_alerts = 0
    conn.close()
    return jsonify({"total_cards": total, "last_sync": last_sync, "total_auto_alerts": total_alerts})



@app.route("/api/sync-progress", methods=["GET"])
def get_sync_progress():
    import sync_all
    return jsonify(sync_all.sync_progress)

@app.route("/api/run-sync", methods=["POST"])
def run_sync():
    import threading, sync_all
    def _sync():
        try:
            sync_all.run_full_sync()
        except Exception as e:
            logging.error(f"Sync falhou: {e}")
    threading.Thread(target=_sync, daemon=True, name="full-sync").start()
    return jsonify({"ok": True, "status": "sync_started"})


@app.route("/api/all-cards", methods=["GET"])
def get_all_cards_api():
    page  = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    q     = request.args.get("q", "").strip().lower()
    offset = (page - 1) * limit
    conn = db.get_conn()
    c = conn.cursor()
    try:
        if q:
            c.execute("""
                SELECT * FROM all_cards
                WHERE lower(name) LIKE ? OR lower(collector_number) LIKE ?
                ORDER BY ABS(variation_pct) DESC LIMIT ? OFFSET ?
            """, (f"%{q}%", f"%{q}%", limit, offset))
        else:
            c.execute("SELECT * FROM all_cards ORDER BY ABS(variation_pct) DESC LIMIT ? OFFSET ?", (limit, offset))
        rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT COUNT(*) as total FROM all_cards")
        total = c.fetchone()["total"]
    except Exception:
        rows = []; total = 0
    conn.close()
    return jsonify({"cards": rows, "total": total})

@app.route("/api/test-telegram", methods=["POST"])
def test_telegram():
    import telegram_notify
    data = request.json or {}
    token   = data.get("token")   or db.get_setting("telegram_token")
    chat_id = data.get("chat_id") or db.get_setting("telegram_chat_id")
    ok = telegram_notify.test_connection(token, chat_id)
    return jsonify({"ok": ok})


# ─── Boot ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🏴‍☠️  OP TCG Monitor rodando em http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)