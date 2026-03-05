import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.core.config import settings


logger = logging.getLogger(__name__)


def _send_via_mailersend(to: str, subject: str, html_body: str) -> bool:
  """
  Try to send an email using MailerSend. Returns True on success.
  """
  api_key = getattr(settings, "MAILERSEND_API_KEY", None)
  from_email = getattr(settings, "MAILERSEND_FROM_EMAIL", None) or getattr(settings, "EMAIL_FROM", None)
  if not api_key or not from_email:
    return False

  data = {
    "from": {"email": from_email, "name": "Gap Detector"},
    "to": [{"email": to}],
    "subject": subject,
    "text": html_body,
    "html": html_body,
  }
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  try:
    with httpx.Client(timeout=10.0) as client:
      resp = client.post("https://api.mailersend.com/v1/email", json=data, headers=headers)
    if resp.status_code >= 300:
      logger.warning("MailerSend email failed (%s): %s", resp.status_code, resp.text)
      return False
    return True
  except Exception as exc:  # pragma: no cover - defensive
    logger.exception("MailerSend email error: %s", exc)
    return False


def send_email(to: str, subject: str, html_body: str) -> None:
  """Send an email via MailerSend or SMTP, or log it in dev."""
  # Prefer MailerSend when configured.
  if _send_via_mailersend(to, subject, html_body):
    return

  msg = EmailMessage()
  msg["From"] = settings.EMAIL_FROM
  msg["To"] = to
  msg["Subject"] = subject
  msg.set_content(html_body, subtype="html")

  if not settings.SMTP_HOST:
    # Local/dev fallback
    logger.info("Email to %s: %s\n%s", to, subject, html_body)
    return

  try:
    if settings.SMTP_TLS:
      with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        if settings.SMTP_USERNAME:
          server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
    else:
      with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        if settings.SMTP_USERNAME:
          server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
  except Exception as exc:  # pragma: no cover - defensive
    logger.exception("Failed to send email to %s: %s", to, exc)

