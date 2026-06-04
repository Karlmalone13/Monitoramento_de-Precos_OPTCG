import threading
import logging
import time
from datetime import datetime

import database as db
import scrapers
import alerts as alert_engine

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _run_cycle():
    """Fetch fresh prices for every manually monitored card."""
    cards = db.get_all_cards()
    if not cards:
        logger.info("[Scheduler] Nenhuma carta monitorada.")
        return

    logger.info(f"[Scheduler] Iniciando ciclo — {len(cards)} carta(s)")

    for card in cards:
        card_id   = card["id"]
        card_name = card["name"] or card.get("card_code", "")

        try:
            results = scrapers.scrape_all(card_name)
        except Exception as e:
            logger.error(f"[Scheduler] Erro ao scraping '{card_name}': {e}")
            continue

        for source, data in results.items():
            price    = data.get("price")
            currency = data.get("currency", "EUR")
            url      = data.get("url", "")
            error    = data.get("error")

            if error:
                logger.warning(f"[Scheduler] {source}/{card_name}: {error}")

            if price is not None:
                db.save_price(card_id, source, price, currency, url)
                alert_engine.check_and_alert(card_id, card_name, source, price, currency)

            if source == "cardtrader" and data.get("image_url"):
                db.update_card_image(card_id, data["image_url"])
            if source == "cardtrader" and data.get("display_name"):
                db.update_card_display_name(card_id, data["display_name"])

    logger.info("[Scheduler] Ciclo manual concluído.")


def _should_run_daily_sync():
    """Verifica se já passou 24h desde a última sincronização completa."""
    last = db.get_setting("last_full_sync")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        diff = (datetime.utcnow() - last_dt).total_seconds()
        return diff >= 86400  # 24 horas
    except Exception:
        return True

_sync_running = False

def _is_syncing():
    return _sync_running

def _loop():
    while not _stop_event.is_set():
        try:
            _run_cycle()
        except Exception as e:
            logger.error(f"[Scheduler] Erro no ciclo manual: {e}")

        # Verifica se precisa rodar a sync completa
        try:
         if _should_run_daily_sync() and not _is_syncing():
             logger.info("[Scheduler] Iniciando sincronização diária automática...")
             import sync_all
             sync_all.run_full_sync()
             logger.info("[Scheduler] Sincronização diária concluída.")
        except Exception as e:
             logger.error(f"[Scheduler] Erro na sync diária: {e}")

        interval_min = int(db.get_setting("check_interval_min") or 30)
        logger.info(f"[Scheduler] Aguardando {interval_min} min até o próximo ciclo.")

        for _ in range(interval_min * 60):
            if _stop_event.is_set():
                break
            time.sleep(1)


def start():
    global _thread, _stop_event
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="price-scheduler")
    _thread.start()
    logger.info("[Scheduler] Iniciado.")


def stop():
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
    logger.info("[Scheduler] Parado.")


def run_now():
    """Trigger an immediate manual scraping cycle (non-blocking)."""
    t = threading.Thread(target=_run_cycle, daemon=True, name="price-cycle-now")
    t.start()
    return t