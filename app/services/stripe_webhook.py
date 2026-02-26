"""
Stripe webhook entitlement logic. Idempotent: safe to process the same event twice.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entitlement import Entitlement
from app.models.scan import Scan
from app.models.user import User
from app.services import auth as auth_service

logger = logging.getLogger(__name__)

MONTHLY_QUOTA_SUB = 8


def _get_price_id_from_session(session: Any) -> Optional[str]:
  """Extract the first line item price id from a Checkout Session."""
  if not session:
    return None
  line_items = getattr(session, "line_items", None)
  if line_items and hasattr(line_items, "data") and line_items.data:
    first = line_items.data[0]
    price = getattr(first, "price", None)
    if price:
      return getattr(price, "id", None)
  return None


def _get_price_id_from_invoice(invoice: Any) -> Optional[str]:
  """Extract the first line item price id from an Invoice."""
  if not invoice or not getattr(invoice, "lines", None):
    return None
  lines = invoice.lines
  if hasattr(lines, "data") and lines.data:
    first = lines.data[0]
    price = getattr(first, "price", None)
    if price:
      return getattr(price, "id", None)
  return None


async def get_or_create_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
  """Get user by email or create if missing. Returns None if email empty."""
  email = (email or "").strip().lower()
  if not email:
    return None
  result = await db.execute(select(User).where(User.email == email).limit(1))
  user = result.scalar_one_or_none()
  if user:
    return user
  user = User(email=email)
  db.add(user)
  await db.flush()
  logger.info("Created user from Stripe webhook: %s", email)
  return user


async def get_or_create_entitlement(db: AsyncSession, user_id: UUID) -> Entitlement:
  """One entitlement per user."""
  result = await db.execute(select(Entitlement).where(Entitlement.user_id == user_id).limit(1))
  ent = result.scalar_one_or_none()
  if ent:
    return ent
  ent = Entitlement(user_id=user_id)
  db.add(ent)
  await db.flush()
  logger.info("Created entitlement for user_id=%s", user_id)
  return ent


async def apply_checkout_session_completed(db: AsyncSession, session: Any) -> None:
  """
  Grant entitlements from checkout.session.completed.
  Idempotent: we update by amount (credits_remaining += N) and set subscription state;
  duplicate events would double-grant unless we track processed session IDs (we don't here;
  for production add a processed_events table or check Stripe idempotency).
  For true idempotency you'd store stripe_checkout_session_id and skip if already applied.
  """
  customer_email = getattr(session, "customer_email", None) or getattr(session, "customer_details", None)
  if customer_email and hasattr(customer_email, "email"):
    customer_email = customer_email.email
  if not customer_email and getattr(session, "customer", None):
    try:
      cust = stripe.Customer.retrieve(session.customer)
      customer_email = getattr(cust, "email", None)
    except Exception as e:
      logger.warning("Could not retrieve Stripe customer for email: %s", e)
  if not customer_email:
    logger.warning("checkout.session.completed: no customer_email or customer, skipping")
    return

  user = await get_or_create_user_by_email(db, customer_email)
  if not user:
    return
  entitlement = await get_or_create_entitlement(db, user.id)

  stripe_customer_id = getattr(session, "customer", None)
  if stripe_customer_id:
    entitlement.stripe_customer_id = stripe_customer_id
    user.stripe_customer_id = stripe_customer_id

  price_id = _get_price_id_from_session(session)
  if not price_id:
    # Expand line_items if not present
    try:
      expanded = stripe.checkout.Session.retrieve(
        session.id,
        expand=["line_items.data.price"],
      )
      price_id = _get_price_id_from_session(expanded)
    except Exception as e:
      logger.warning("Could not expand session line_items: %s", e)

  if price_id == (settings.PRICE_SINGLE_REPORT or settings.STRIPE_PRICE_ID):
    entitlement.credits_remaining = (entitlement.credits_remaining or 0) + 1
    logger.info("Granted 1 credit to user_id=%s (single report)", user.id)
  elif price_id == settings.PRICE_BUNDLE_5:
    entitlement.credits_remaining = (entitlement.credits_remaining or 0) + 5
    logger.info("Granted 5 credits to user_id=%s (bundle)", user.id)
  elif price_id == settings.PRICE_SUB_MONTHLY:
    entitlement.subscription_active = True
    entitlement.monthly_quota = MONTHLY_QUOTA_SUB
    entitlement.credits_remaining = MONTHLY_QUOTA_SUB
    sub_id = getattr(session, "subscription", None)
    if sub_id:
      try:
        sub = stripe.Subscription.retrieve(sub_id)
        if sub.current_period_end:
          entitlement.subscription_renewal_date = datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc)
      except Exception as e:
        logger.warning("Could not retrieve subscription for renewal date: %s", e)
    user.stripe_subscription_id = sub_id
    logger.info("Activated subscription for user_id=%s (monthly 8 credits)", user.id)
  else:
    logger.warning("Unknown price_id in checkout.session.completed: %s", price_id)

  # If this user doesn't yet have a password set, send a password-reset link so they
  # can create one and access their credits later. This doubles as a "magic link" to
  # finish account setup after purchasing via Stripe without prior login.
  try:
    if not user.hashed_password:
      await auth_service.request_password_reset(db, user.email)
  except Exception as e:
    logger.warning("Could not send post-purchase password setup email: %s", e)

  # Unlock scan if client_reference_id is a scan_id (single-report purchase for that scan)
  ref = getattr(session, "client_reference_id", None)
  if ref:
    try:
      scan_uuid = UUID(ref)
      scan = await db.get(Scan, scan_uuid)
      if scan:
        scan.is_unlocked = True
        if scan.user_id is None:
          scan.user_id = user.id
        logger.info("Unlocked scan_id=%s from checkout", scan_uuid)
    except (ValueError, TypeError):
      pass

  await db.commit()


async def apply_invoice_paid(db: AsyncSession, invoice: Any) -> None:
  """Subscription renewal: reset credits to monthly_quota."""
  price_id = _get_price_id_from_invoice(invoice)
  if price_id != settings.PRICE_SUB_MONTHLY:
    return
  sub_id = getattr(invoice, "subscription", None)
  if not sub_id:
    return
  try:
    sub = stripe.Subscription.retrieve(sub_id)
    customer_id = getattr(sub, "customer", None)
  except Exception as e:
    logger.warning("Could not retrieve subscription on invoice.paid: %s", e)
    return

  result = await db.execute(
    select(Entitlement).where(Entitlement.stripe_customer_id == customer_id).limit(1)
  )
  ent = result.scalar_one_or_none()
  if not ent:
    return
  ent.credits_remaining = ent.monthly_quota or MONTHLY_QUOTA_SUB
  if sub.current_period_end:
    ent.subscription_renewal_date = datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc)
  await db.commit()
  logger.info("Renewed subscription credits for entitlement id=%s", ent.id)


async def apply_subscription_deleted_or_payment_failed(
  db: AsyncSession,
  subscription_id: Optional[str] = None,
  customer_id: Optional[str] = None,
) -> None:
  """Set subscription_active = False. Pass subscription_id or customer_id."""
  if subscription_id:
    try:
      sub = stripe.Subscription.retrieve(subscription_id)
      customer_id = getattr(sub, "customer", None)
    except Exception as e:
      logger.warning("Could not retrieve subscription: %s", e)
      return
  if not customer_id:
    return
  result = await db.execute(
    select(Entitlement).where(Entitlement.stripe_customer_id == customer_id).limit(1)
  )
  ent = result.scalar_one_or_none()
  if not ent:
    return
  ent.subscription_active = False
  user = await db.get(User, ent.user_id)
  if user:
    user.stripe_subscription_id = None
  await db.commit()
  logger.info("Deactivated subscription for user_id=%s", ent.user_id)
