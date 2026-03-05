from fastapi import APIRouter, HTTPException, status
from fastapi import Request
from pydantic import BaseModel, EmailStr
import httpx

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contact", tags=["contact"])


class ContactRequest(BaseModel):
  email: EmailStr | None = None
  message: str
  page: str | None = None


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def send_contact(request: Request, payload: ContactRequest) -> None:
  """
  Lightweight contact endpoint that forwards messages to a configured
  internal address via MailerSend.
  """
  api_key = settings.MAILERSEND_API_KEY
  from_email = settings.MAILERSEND_FROM_EMAIL or settings.MAILERSEND_TO_EMAIL
  to_email = settings.MAILERSEND_TO_EMAIL or settings.MAILERSEND_FROM_EMAIL

  if not api_key or not from_email or not to_email:
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail="Contact form email is not configured. Set MAILERSEND_API_KEY, MAILERSEND_FROM_EMAIL, and MAILERSEND_TO_EMAIL.",
    )

  subject = "New Gap Detector contact form message"
  requester = payload.email or "Anonymous"
  page = payload.page or str(request.url.path)

  text_body = (
    f"New contact form message from: {requester}\n"
    f"Page: {page}\n\n"
    f"Message:\n{payload.message}\n"
  )

  html_body = (
    "<p><strong>New contact form message</strong></p>"
    f"<p><strong>From:</strong> {requester}<br>"
    f"<strong>Page:</strong> {page}</p>"
    "<p><strong>Message:</strong></p>"
    f"<p>{payload.message.replace(chr(10), '<br>')}</p>"
  )

  data = {
    "from": {"email": from_email, "name": "Gap Detector"},
    "to": [{"email": to_email}],
    "subject": subject,
    "text": text_body,
    "html": html_body,
  }

  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }

  try:
    async with httpx.AsyncClient(timeout=10.0) as client:
      resp = await client.post("https://api.mailersend.com/v1/email", json=data, headers=headers)
    if resp.status_code >= 300:
      logger.warning("MailerSend contact send failed: %s %s", resp.status_code, resp.text)
      raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Unable to send message right now. Please try again later.",
      )
  except HTTPException:
    raise
  except Exception as exc:  # pragma: no cover - defensive
    logger.exception("MailerSend contact send error: %s", exc)
    raise HTTPException(
      status_code=status.HTTP_502_BAD_GATEWAY,
      detail="Unable to send message right now. Please try again later.",
    ) from exc

