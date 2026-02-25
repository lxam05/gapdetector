import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings


logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, html_body: str) -> None:
  """Send an email or log it in local/dev environments.

  For Railway, configure SMTP_* environment variables.
  """
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

