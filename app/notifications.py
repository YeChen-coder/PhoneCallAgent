"""Outbound SMS (appointment confirmations). No-op unless SEND_SMS_CONFIRMATIONS=true."""

from __future__ import annotations

import logging

from app.config import get_settings

log = logging.getLogger(__name__)


def send_sms(to: str | None, body: str) -> None:
    s = get_settings()
    if not (s.send_sms_confirmations and to and s.twilio_account_sid):
        log.info("SMS skipped (disabled or no number): %s", body)
        return
    from twilio.rest import Client

    try:
        Client(s.twilio_account_sid, s.twilio_auth_token).messages.create(to=to, from_=s.twilio_phone_number, body=body)
    except Exception:  # never let an SMS failure break the call
        log.exception("Failed to send SMS to %s", to)
