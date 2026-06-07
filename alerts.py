"""
alerts.py — Compares new prices to previous ones and fires alerts when
the variation exceeds the configured threshold. Also handles email sending.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import database as db
import telegram_notify

logger = logging.getLogger(__name__)

SOURCE_LABELS = {
    "liga":       "Liga One Piece",
    "cardmarket": "CardMarket",
    "cardtrader": "CardTrader",
}

CURRENCY_SYMBOLS = {
    "BRL": "R$",
    "EUR": "€",
}


def check_and_alert(card_id: int, card_name: str, source: str,
                    new_price: float, currency: str) -> dict | None:
    """
    Compare new_price to the previous recorded price for this card+source.
    If variation >= threshold, save an alert and return it. Otherwise None.
    """
    threshold = float(db.get_setting("threshold_manual_pct") or db.get_setting("threshold_pct") or 20)
    old_price = db.get_previous_price(card_id, source)

    if old_price is None or old_price == 0:
        return None  # no baseline yet

    variation_pct = ((new_price - old_price) / old_price) * 100

    if abs(variation_pct) < threshold:
        return None

    direction = "up" if variation_pct > 0 else "down"
    db.save_alert(card_id, source, direction, old_price, new_price, round(variation_pct, 2))

    # Telegram notification
    try:
        tok = db.get_setting("telegram_token") or ""
        cid = db.get_setting("telegram_chat_id") or ""
        if tok and cid:
            sym = CURRENCY_SYMBOLS.get(currency, currency)
            telegram_notify.send_price_alert(
                card_name=card_name,
                collector=card_name,
                expansion="",
                direction=direction,
                old_price=old_price,
                new_price=new_price,
                variation_pct=variation_pct,
                source=SOURCE_LABELS.get(source, source),
                token=tok,
                chat_id=cid
            )
    except Exception as e:
        logger.error(f"[Telegram] Falha no alerta manual: {e}")

    alert = {
        "card_id":       card_id,
        "card_name":     card_name,
        "source":        SOURCE_LABELS.get(source, source),
        "direction":     direction,
        "old_price":     old_price,
        "new_price":     new_price,
        "currency":      currency,
        "variation_pct": round(variation_pct, 2),
        "created_at":    datetime.utcnow().isoformat(),
    }

    logger.warning(
        f"[ALERT] {card_name} @ {source}: "
        f"{old_price} → {new_price} ({variation_pct:+.1f}%)"
    )

    _maybe_send_email(alert)
    return alert


def _maybe_send_email(alert: dict):
    """Send an email notification if SMTP is configured."""
    email_to   = db.get_setting("email_to") or ""
    email_from = db.get_setting("email_from") or ""
    password   = db.get_setting("smtp_password") or ""
    smtp_host  = db.get_setting("smtp_host") or "smtp.gmail.com"
    smtp_port  = int(db.get_setting("smtp_port") or 587)

    if not all([email_to, email_from, password]):
        return  # email not configured

    sym = CURRENCY_SYMBOLS.get(alert["currency"], alert["currency"])
    arrow = "📈" if alert["direction"] == "up" else "📉"
    direction_label = "SUBIU" if alert["direction"] == "up" else "BAIXOU"

    subject = (
        f"{arrow} {alert['card_name']} {direction_label} "
        f"{abs(alert['variation_pct']):.1f}% no {alert['source']}"
    )

    body_html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto;">
      <h2 style="color: {'#c0392b' if alert['direction'] == 'up' else '#27ae60'}">
        {arrow} Alerta de Preço — One Piece TCG
      </h2>
      <table style="width:100%; border-collapse: collapse;">
        <tr><td style="padding:8px; border-bottom:1px solid #eee;"><b>Carta</b></td>
            <td style="padding:8px; border-bottom:1px solid #eee;">{alert['card_name']}</td></tr>
        <tr><td style="padding:8px; border-bottom:1px solid #eee;"><b>Site</b></td>
            <td style="padding:8px; border-bottom:1px solid #eee;">{alert['source']}</td></tr>
        <tr><td style="padding:8px; border-bottom:1px solid #eee;"><b>Preço anterior</b></td>
            <td style="padding:8px; border-bottom:1px solid #eee;">{sym}{alert['old_price']:.2f}</td></tr>
        <tr><td style="padding:8px; border-bottom:1px solid #eee;"><b>Preço atual</b></td>
            <td style="padding:8px; border-bottom:1px solid #eee;"><b>{sym}{alert['new_price']:.2f}</b></td></tr>
        <tr><td style="padding:8px;"><b>Variação</b></td>
            <td style="padding:8px; color: {'#c0392b' if alert['direction'] == 'up' else '#27ae60'};">
              <b>{alert['variation_pct']:+.2f}%</b></td></tr>
      </table>
      <p style="color:#888; font-size:12px; margin-top:16px;">
        OP TCG Monitor • {alert['created_at'][:19].replace('T',' ')} UTC
      </p>
    </body></html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_from
        msg["To"]      = email_to
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(email_from, password)
            server.sendmail(email_from, [email_to], msg.as_string())

        logger.info(f"Email de alerta enviado para {email_to}")
    except Exception as e:
        logger.error(f"Falha ao enviar email: {e}")