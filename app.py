"""
app.py — Flask REST API for the One Piece TCG Price Monitor.
"""

import logging
import os
import threading
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, Response

import database as db
import scrapers
import alerts as alert_engine
import scheduler

# ─── Auth ────────────────────────────────────────────────────────────────────

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "optcg2026")

def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                'Acesso negado.',
                401,
                {'WWW-Authenticate': 'Basic realm="OP TCG Monitor"'}
            )
        return f(*args, **kwargs)
    return decorated

# ─── App ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__, static_folder="frontend", static_url_path="")

# ─── Serve frontend ───────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    return send_from_directory("frontend", "index.html")


# ─── Cards ────────────────────────────────────────────────────────────────────

@app.route("/api/cards", methods=["GET"])
@require_auth
def get_cards():
    cards = db.get_all_cards()
    enriched = []
    for card in cards:
        latest = db.get_latest_prices(card["id"])
        enriched.append({**card, "latest_prices": latest})
    return jsonify(enriched)

@app.route("/api/cards", methods=["POST"])
@require_auth
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
                if source == "cardtrader" and data.get("display_name"):
                    db.update_card_display_name(card_id, data["display_name"])
                if source == "cardtrader" and data.get("expansion_name"):
                    db.update_card_expansion(card_id, data["expansion_name"])
        except Exception as e:
            logging.error(f"Scrape imediato falhou: {e}")

    threading.Thread(target=_scrape, daemon=True).start()

    return jsonify({"id": card_id, "name": label, "status": "scraping_started"}), 201


@app.route("/api/cards/<int:card_id>", methods=["DELETE"])
@require_auth
def delete_card(card_id):
    db.remove_card(card_id)
    return jsonify({"ok": True})


# ─── Prices ───────────────────────────────────────────────────────────────────

@app.route("/api/cards/<int:card_id>/prices", methods=["GET"])
@require_auth
def get_prices(card_id):
    latest  = db.get_latest_prices(card_id)
    history = db.get_price_history(card_id, limit=100)
    return jsonify({"latest": latest, "history": history})


@app.route("/api/cards/<int:card_id>/refresh", methods=["POST"])
@require_auth
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
                if source == "cardtrader" and data.get("display_name"):
                    db.update_card_display_name(card_id, data["display_name"])
                if source == "cardtrader" and data.get("expansion_name"):
                    db.update_card_expansion(card_id, data["expansion_name"])
        except Exception as e:
            logging.error(f"Refresh falhou para card {card_id}: {e}")

    threading.Thread(target=_scrape, daemon=True).start()
    return jsonify({"ok": True, "status": "scraping_started"})


@app.route("/api/refresh-all", methods=["POST"])
@require_auth
def refresh_all():
    scheduler.run_now()
    return jsonify({"ok": True, "status": "cycle_started"})


# ─── Alerts ───────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
@require_auth
def get_alerts():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_alerts(limit))


# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    settings = db.get_all_settings()
    # Never return the SMTP password
    settings.pop("smtp_password", None)
    return jsonify(settings)


@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    data = request.json or {}
    allowed = {
        "threshold_pct", "threshold_manual_pct", "check_interval_min",
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
@require_auth
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
@require_auth
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
@require_auth
def get_sync_progress():
    import sync_all
    return jsonify(sync_all.sync_progress)

@app.route("/api/run-sync", methods=["POST"])
@require_auth
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
@require_auth
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
@require_auth
def test_telegram():
    import telegram_notify
    data = request.json or {}
    token   = data.get("token")   or db.get_setting("telegram_token")
    chat_id = data.get("chat_id") or db.get_setting("telegram_chat_id")
    ok = telegram_notify.test_connection(token, chat_id)
    return jsonify({"ok": ok})

@app.route("/api/search-cards", methods=["GET"])
@require_auth
def search_cards():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            SELECT collector_number, name, expansion_name, image_url
            FROM all_cards
            WHERE lower(name) LIKE ? OR lower(collector_number) LIKE ?
            ORDER BY collector_number
            LIMIT 10
        """, (f"%{q}%", f"%{q}%"))
        rows = [dict(r) for r in c.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return jsonify(rows)

# ─── Boot ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🏴‍☠️  OP TCG Monitor rodando em http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)