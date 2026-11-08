"""Send outage and recovery notices by email (SMTP) and by SMS (generic HTTPS webhook)."""

import logging
import smtplib
from email.message import EmailMessage

import httpx

logger = logging.getLogger("tollgate.notifier")


class Notifier:
    def __init__(self, settings, transport=None, smtp_factory=smtplib.SMTP):
        self.settings, self.transport, self.smtp_factory = settings, transport, smtp_factory

    @property
    def email_enabled(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    @property
    def sms_enabled(self) -> bool:
        return bool(self.settings.sms_webhook_url)

    def send_email(self, to: str, subject: str, body: str) -> bool:
        if not self.email_enabled:
            return False
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.settings.smtp_from, to, subject
        message.set_content(body)
        try:
            with self.smtp_factory(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if self.settings.smtp_username:
                    smtp.login(
                        self.settings.smtp_username,
                        self.settings.smtp_password.get_secret_value(),
                    )
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException) as exc:
            logger.error("Email failed exception_type=%s", type(exc).__name__)
            return False

    def send_sms(self, to: str, body: str) -> bool:
        """POST {"to","message"} as JSON; adapt the receiving service to your SMS provider."""
        if not self.sms_enabled:
            return False
        headers = {}
        token = self.settings.sms_webhook_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=15, transport=self.transport, trust_env=False) as client:
                response = client.post(
                    self.settings.sms_webhook_url, json={"to": to, "message": body}, headers=headers
                )
            return response.is_success
        except httpx.HTTPError as exc:
            logger.error("SMS failed exception_type=%s", type(exc).__name__)
            return False

    def notify(self, contacts: list[dict], subject: str, body: str) -> dict:
        """Best effort for every contact; one failure never blocks the others."""
        sent = {"email": 0, "sms": 0, "failed": 0}
        for contact in contacts:
            for channel, address in (("email", contact.get("email")), ("sms", contact.get("phone"))):
                if not address:
                    continue
                ok = (
                    self.send_email(address, subject, body)
                    if channel == "email"
                    else self.send_sms(address, f"{subject}: {body}"[:300])
                )
                sent[channel if ok else "failed"] += 1
        return sent
