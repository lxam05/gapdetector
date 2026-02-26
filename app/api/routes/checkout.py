from typing import Optional

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_optional_user
from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["checkout"])


class GenericCheckoutRequest(BaseModel):
  plan: str


class CheckoutSessionOut(BaseModel):
  url: str


def _resolve_plan_to_price_and_mode(plan: str) -> tuple[str, str]:
  """
  Map a logical plan name to a Stripe Price ID and Checkout mode.

  Plans:
    - single        -> PRICE_SINGLE_REPORT (or legacy STRIPE_PRICE_ID), mode=payment
    - bundle        -> PRICE_BUNDLE_5, mode=payment
    - subscription  -> PRICE_SUB_MONTHLY, mode=subscription
  """
  chosen = (plan or "single").lower()
  mode = "payment"

  if chosen == "bundle":
    price_id = settings.PRICE_BUNDLE_5
    if not price_id:
      raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="PRICE_BUNDLE_5 not set.",
      )
  elif chosen in ("subscription", "monthly", "sub"):
    mode = "subscription"
    price_id = settings.PRICE_SUB_MONTHLY
    if not price_id:
      raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="PRICE_SUB_MONTHLY not set.",
      )
  else:
    price_id = settings.PRICE_SINGLE_REPORT or settings.STRIPE_PRICE_ID
    if not price_id:
      raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="PRICE_SINGLE_REPORT or STRIPE_PRICE_ID not set.",
      )

  return price_id, mode


@router.post("/checkout", response_model=CheckoutSessionOut)
async def create_generic_checkout_session(
  payload: GenericCheckoutRequest,
  current_user: Optional[User] = Depends(get_optional_user),
) -> CheckoutSessionOut:
  """
  Create a generic Stripe Checkout Session for purchasing credits / subscription.

  Does NOT depend on a scan_id and can be used from the pricing page directly.
  """
  if not settings.STRIPE_SECRET_KEY:
    logger.warning("Checkout 503: STRIPE_SECRET_KEY not set.")
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail="Checkout not configured. Set STRIPE_SECRET_KEY and Price IDs in .env and restart the server.",
    )

  price_id, mode = _resolve_plan_to_price_and_mode(payload.plan)

  try:
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base = settings.SUCCESS_URL_BASE.rstrip("/")
    success_url = f"{base}/post-purchase"
    cancel_url = f"{base}/pricing"

    metadata = {"plan": (payload.plan or "single").lower()}

    create_kw: dict = {
      "mode": mode,
      "line_items": [{"price": price_id, "quantity": 1}],
      "success_url": success_url,
      "cancel_url": cancel_url,
      "metadata": metadata,
    }
    if current_user and getattr(current_user, "email", None):
      create_kw["customer_email"] = current_user.email

    session = stripe.checkout.Session.create(**create_kw)
    if not getattr(session, "url", None):
      raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Stripe did not return a checkout URL.",
      )
    return CheckoutSessionOut(url=session.url)
  except HTTPException:
    raise
  except Exception as e:
    logger.exception("Generic Stripe checkout session create failed: %s", e)
    err_msg = str(e).strip() if str(e) else "Unknown error"
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail=f"Checkout failed: {err_msg}. Check STRIPE_SECRET_KEY and Price IDs (use test keys and Price IDs from the same Stripe account).",
    ) from e

