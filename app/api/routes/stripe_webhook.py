"""
Stripe webhook endpoint. Verifies signature and delegates to entitlement handlers.
"""
import logging
from types import SimpleNamespace
from typing import Any

import stripe
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services import stripe_webhook as svc

router = APIRouter(prefix="/stripe", tags=["stripe"])
logger = logging.getLogger(__name__)


def _obj(d: dict) -> Any:
  if d is None:
    return None
  out = SimpleNamespace()
  for k, v in d.items():
    setattr(out, k, _obj(v) if isinstance(v, dict) else v)
  return out


@router.post("/webhook")
async def stripe_webhook(
  request: Request,
  db: AsyncSession = Depends(get_db),
) -> Response:
  """
  Stripe webhook. Verifies signature with STRIPE_WEBHOOK_SECRET and handles
  checkout.session.completed, invoice.paid, customer.subscription.deleted, invoice.payment_failed.
  Returns 200 for all handled and unknown events (so Stripe does not retry).
  """
  payload = await request.body()
  sig_header = request.headers.get("stripe-signature", "")
  webhook_secret = settings.STRIPE_WEBHOOK_SECRET

  if not webhook_secret:
    logger.warning("STRIPE_WEBHOOK_SECRET not set; rejecting webhook")
    return Response(status_code=500, content="Webhook secret not configured")

  try:
    event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
  except ValueError as e:
    logger.warning("Stripe webhook invalid payload: %s", e)
    return Response(status_code=400, content="Invalid payload")
  except stripe.SignatureVerificationError as e:
    logger.warning("Stripe webhook signature verification failed: %s", e)
    return Response(status_code=400, content="Invalid signature")

  logger.info("Stripe webhook event: %s (id=%s)", event.get("type"), event.get("id"))

  try:
    if event["type"] == "checkout.session.completed":
      session = _obj(event["data"]["object"])
      if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY
      await svc.apply_checkout_session_completed(db, session)
    elif event["type"] == "invoice.paid":
      invoice = _obj(event["data"]["object"])
      if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY
      await svc.apply_invoice_paid(db, invoice)
    elif event["type"] == "customer.subscription.deleted":
      sub = event["data"]["object"]
      sub_id = sub.get("id")
      if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY
      await svc.apply_subscription_deleted_or_payment_failed(db, subscription_id=sub_id)
    elif event["type"] == "invoice.payment_failed":
      invoice = event["data"]["object"]
      customer_id = invoice.get("customer")
      if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY
      await svc.apply_subscription_deleted_or_payment_failed(db, customer_id=customer_id)
  except Exception as e:
    logger.exception("Webhook handler error for %s: %s", event.get("type"), e)
    return Response(status_code=500, content="Handler error")

  return Response(status_code=200, content="ok")
