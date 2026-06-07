"""
telegram_notify.py — Envia notificações via Telegram Bot API.
"""

import urllib.request
import urllib.parse
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(text: str, token: str = None, chat_id: str = None) -> bool:
    """Envia uma mensagem de texto via Telegram."""
    tok  = token   or TELEGRAM_TOKEN
    cid  = chat_id or TELEGRAM_CHAT_ID
    if not tok or not cid:
        logger.warning("[Telegram] Token ou Chat ID não configurado.")
        return False

    url  = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = json.dumps({
        "chat_id":    cid,
        "text":       text,
        "parse_mode": "HTML"
    }).encode()

    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                logger.info(f"[Telegram] Mensagem enviada para {cid}")
                return True
            else:
                logger.error(f"[Telegram] Erro: {result}")
                return False
    except Exception as e:
        logger.error(f"[Telegram] Falha ao enviar: {e}")
        return False


def send_price_alert(card_name: str, collector: str, expansion: str,
                     direction: str, old_price: float, new_price: float,
                     variation_pct: float, source: str = "CardTrader",
                     image_url: str = None,
                     token: str = None, chat_id: str = None):
    """Formata e envia um alerta de variação de preço."""
    arrow   = "📈" if direction == "up" else "📉"
    emoji   = "🔴" if direction == "up" else "🟢"
    dir_txt = "SUBIU" if direction == "up" else "BAIXOU"
    sym     = "R$" if source == "liga" else "€"
    pct     = abs(variation_pct)

    text = (
        f"{arrow} <b>Alerta de Preço — One Piece TCG</b>\n\n"
        f"{emoji} <b>{card_name}</b> {dir_txt} <b>{pct:.1f}%</b>\n"
        f"{'🃏 ' + collector if collector else ''}"
        f"{(' · ' + expansion) if expansion else ''}\n\n"
        f"💰 <b>{sym}{old_price:.2f}</b> → <b>{sym}{new_price:.2f}</b>\n"
        f"📊 Fonte: {source}\n"
        f"🏴‍☠️ <i>OP TCG Price Monitor</i>"
    )

    # Tenta enviar com foto, fallback para texto
    if image_url:
        ok = send_photo(image_url, text, token, chat_id)
        if ok:
            return True
    return send_message(text, token, chat_id)


def test_connection(token: str = None, chat_id: str = None) -> bool:
    """Envia mensagem de teste."""
    return send_message(
        "✅ <b>OP TCG Price Monitor</b>\n\nConexão com Telegram configurada com sucesso! 🏴‍☠️",
        token, chat_id
    )

def send_photo(image_url: str, caption: str, token: str = None, chat_id: str = None) -> bool:
    """Envia uma foto com legenda via Telegram."""
    tok = token or TELEGRAM_TOKEN
    cid = chat_id or TELEGRAM_CHAT_ID
    if not tok or not cid:
        return False

    url = f"https://api.telegram.org/bot{tok}/sendPhoto"
    data = json.dumps({
        "chat_id": cid,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML"
    }).encode()

    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception as e:
        logger.error(f"[Telegram] Falha ao enviar foto: {e}")
        return False