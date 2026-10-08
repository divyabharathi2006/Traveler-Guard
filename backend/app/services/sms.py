from dataclasses import dataclass
import logging

from ..config import settings

logger = logging.getLogger("travelerguard.sms")


@dataclass(frozen=True)
class SmsResult:
    status: str
    provider_message_id: str | None = None
    error: str | None = None


def twilio_is_configured() -> bool:
    return all(
        (
            settings.notification_provider == "twilio",
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_from_number,
        )
    )


def send_sms(to_number: str, body: str, status_callback: str | None = None) -> SmsResult:
    if settings.app_env.lower() != "production":
        return SmsResult(status="disabled", error="SMS is disabled outside production.")
    if not twilio_is_configured():
        return SmsResult(status="not_configured", error="Twilio credentials or provider configuration are missing.")
    try:
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        params = {
            "body": body,
            "from_": settings.twilio_from_number,
            "to": to_number,
        }
        if status_callback:
            params["status_callback"] = status_callback
        message = client.messages.create(**params)
        return SmsResult(status="sent", provider_message_id=message.sid)
    except Exception:
        logger.exception("Twilio SMS request failed")
        return SmsResult(status="failed", error="Twilio could not deliver the message.")
