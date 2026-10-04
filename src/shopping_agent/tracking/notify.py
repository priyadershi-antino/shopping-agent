"""Deliver queued notifications by email or Telegram. In-app notifications need no delivery.

Errors are reduced to a short reason before they are stored or logged, so tokens embedded in
URLs (Telegram) or SMTP replies never reach the database or the logs.
"""

import asyncio
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

import httpx

from shopping_agent.config import Settings
from shopping_agent.db.models import AlertEventRow
from shopping_agent.db.repositories.tracking import NotificationRepository
from shopping_agent.runtime import Runtime
from shopping_agent.services.alerts import in_quiet_hours

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class DeliveryError(Exception):
    """A delivery failure with a message that is safe to store."""


def _send_email_sync(settings: Settings, alert: AlertEventRow) -> None:
    if not (settings.smtp_host and settings.notify_email_to and settings.smtp_from):
        raise DeliveryError("email is not configured (SMTP_HOST, SMTP_FROM, NOTIFY_EMAIL_TO)")
    msg = EmailMessage()
    msg["Subject"] = f"[Shopping Agent] {alert.title}"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.notify_email_to
    msg.set_content(alert.message + "\n\nYou can review this in the Shopping Agent alerts page.")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"email failed ({type(exc).__name__})") from None


async def send_email(settings: Settings, alert: AlertEventRow) -> None:
    await asyncio.to_thread(_send_email_sync, settings, alert)


async def send_telegram(http: httpx.AsyncClient, settings: Settings, alert: AlertEventRow) -> None:
    token = settings.telegram_bot_token.get_secret_value()
    if not (token and settings.telegram_chat_id):
        raise DeliveryError("Telegram is not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": f"{alert.title}\n\n{alert.message}",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(f"Telegram request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise DeliveryError(f"Telegram returned HTTP {response.status_code}")


async def deliver_pending(rt: Runtime, *, now: datetime) -> int:
    """Send pending email/Telegram notifications. Waits during quiet hours."""
    if in_quiet_hours(now, rt.settings.quiet_hours):
        return 0
    sent = 0
    async with rt.sessions() as session, session.begin():
        for note, alert in await NotificationRepository(session).pending():
            try:
                if note.channel == "email":
                    await send_email(rt.settings, alert)
                elif note.channel == "telegram":
                    await send_telegram(rt.http, rt.settings, alert)
                else:
                    raise DeliveryError(f"unknown channel '{note.channel}'")
            except DeliveryError as exc:
                note.attempts += 1
                note.error = str(exc)
                if note.attempts >= MAX_ATTEMPTS:
                    note.status = "failed"
                log.warning(
                    "notification not delivered", extra={"channel": note.channel, "error": str(exc)}
                )
                continue
            note.status, note.sent_at, note.error = "sent", now, None
            sent += 1
    return sent
