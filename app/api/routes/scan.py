from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.core.config import settings
from app.db.session import get_db
from app.models.entitlement import Entitlement
from app.models.full_scan import FullScan
from app.models.preview_scan import PreviewScan
from app.models.user_scan import UserScan
from app.models.user import User
from app.schemas.scan import ScanCreate, ScanDetailOut, ScanListItem, ScanTeaserOut, ScanUnlockContext
from app.services.scan_analysis import run_full_scan, run_preview_scan
from app.services.stripe_webhook import get_or_create_user_by_email
from app.services.scan_guardrails import (
  enforce_full_global_daily_limit,
  enforce_monthly_api_budget,
  enforce_preview_limits,
  get_client_ip,
  normalize_domain,
  verify_turnstile,
)

router = APIRouter(prefix="/scan", tags=["scan"])
logger = logging.getLogger(__name__)


class CheckoutSessionOut(BaseModel):
  url: str


class ScanCheckoutRequest(BaseModel):
  plan: Optional[str] = None


def _resolve_plan_to_price_and_mode(plan: str) -> tuple[str, str]:
  chosen = (plan or "single").lower()
  mode = "payment"

  if chosen in ("bundle", "bundle5"):
    price_id = settings.PRICE_BUNDLE_5
    if not price_id:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="PRICE_BUNDLE_5 not set.")
  elif chosen in ("subscription", "monthly", "sub"):
    mode = "subscription"
    price_id = settings.PRICE_SUB_MONTHLY
    if not price_id:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="PRICE_SUB_MONTHLY not set.")
  else:
    price_id = settings.PRICE_SINGLE_REPORT or settings.STRIPE_PRICE_ID
    if not price_id:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="PRICE_SINGLE_REPORT or STRIPE_PRICE_ID not set.")

  return price_id, mode


def _cached_full_is_fresh(full_scan: FullScan) -> bool:
  return full_scan.created_at >= datetime.now(timezone.utc) - timedelta(days=7)


def _full_report_has_substance(full_scan: FullScan) -> bool:
  data = full_scan.full_report_json or {}
  if not isinstance(data, dict):
    return False
  if data.get("low_signal") is True:
    return False
  wedge = data.get("dominant_wedge") or data.get("primary_wedge")
  clusters = data.get("complaint_clusters")
  plan = data.get("execution_plan_30_days") or data.get("execution_plan")
  if isinstance(wedge, dict) and isinstance(clusters, list) and isinstance(plan, list):
    key_strength = str(data.get("biggest_strength") or "").strip().lower()
    key_weakness = str(data.get("biggest_weakness") or "").strip().lower()
    bad = ("unable to determine", "access denied", "unknown")
    has_quality_summary = (
      bool(key_strength) and bool(key_weakness)
      and not any(b in key_strength for b in bad)
      and not any(b in key_weakness for b in bad)
    )
    return bool(plan) and bool(wedge.get("what_to_build_first") or wedge.get("first_feature_to_build")) and has_quality_summary

  # Backward-compatibility for older report shape.
  sections = data.get("sections")
  if not isinstance(sections, dict):
    return False
  keys = ["Common Complaints", "Opportunities for a New Competitor", "How to Outperform This Competitor"]
  return any(str(sections.get(key) or "").strip().startswith("- ") for key in keys)


def _build_teaser(preview: PreviewScan, full_scan: Optional[FullScan]) -> ScanTeaserOut:
  data = preview.preview_json or {}
  is_low_signal = bool(data.get("low_signal"))
  low_signal_message = str(data.get("message") or "").strip()
  premium_preview = (
    "Demand is concentrated around a few recurring friction points. "
    "Unlock to see full feature-frequency percentages, demand clusters, monetization gaps, and roadmap."
  )
  return ScanTeaserOut(
    scan_id=preview.id,
    company_name=preview.domain,
    summary=low_signal_message if (is_low_signal and low_signal_message) else "Preview generated from lightweight review sampling.",
    sentiment_score=int(data.get("sentiment_score") or 0),
    opportunity_score=int(data.get("opportunity_score") or 0),
    negative_percent_estimate=int(data.get("negative_percent_estimate") or 0),
    positive_percent_estimate=int(data.get("positive_percent_estimate") or 0),
    key_complaints=[str(x) for x in (data.get("top_pain_points") or [])][:3],
    top_strengths=[str(x) for x in (data.get("top_strengths") or [])][:2],
    recurring_feature_requests_hidden=int(data.get("recurring_feature_requests_hidden") or 0),
    unlock_reviews_count=int(data.get("unlock_reviews_count") or 0),
    locked=full_scan is None,
    premium_preview=premium_preview,
  )


def _build_detail(preview: PreviewScan, full_scan: Optional[FullScan]) -> ScanDetailOut:
  teaser = _build_teaser(preview, full_scan)
  full_dashboard = full_scan.full_report_json if full_scan else None
  premium_analysis = (full_dashboard or {}).get("report") if full_dashboard else None
  return ScanDetailOut(
    scan_id=teaser.scan_id,
    company_name=teaser.company_name,
    summary=teaser.summary,
    sentiment_score=teaser.sentiment_score,
    opportunity_score=teaser.opportunity_score,
    negative_percent_estimate=teaser.negative_percent_estimate,
    positive_percent_estimate=teaser.positive_percent_estimate,
    key_complaints=teaser.key_complaints,
    top_strengths=teaser.top_strengths,
    recurring_feature_requests_hidden=teaser.recurring_feature_requests_hidden,
    unlock_reviews_count=teaser.unlock_reviews_count,
    locked=teaser.locked,
    premium_preview=teaser.premium_preview,
    premium_analysis=premium_analysis,
    created_at=preview.created_at,
    full_dashboard=full_dashboard,
  )


async def _consume_credit_or_402(db: AsyncSession, user_id: UUID) -> None:
  result = await db.execute(select(Entitlement).where(Entitlement.user_id == user_id).limit(1))
  ent = result.scalar_one_or_none()
  remaining = ent.credits_remaining if ent and ent.credits_remaining is not None else 0
  if not ent or remaining <= 0:
    raise HTTPException(
      status_code=status.HTTP_402_PAYMENT_REQUIRED,
      detail="No credits remaining. Purchase credits or a subscription to unlock reports.",
    )
  ent.credits_remaining = remaining - 1
  await db.commit()


async def _get_entitlement_or_402(db: AsyncSession, user_id: UUID) -> Entitlement:
  result = await db.execute(select(Entitlement).where(Entitlement.user_id == user_id).limit(1))
  ent = result.scalar_one_or_none()
  remaining = ent.credits_remaining if ent and ent.credits_remaining is not None else 0
  if not ent or remaining <= 0:
    raise HTTPException(
      status_code=status.HTTP_402_PAYMENT_REQUIRED,
      detail="No credits remaining. Purchase credits or a subscription to unlock reports.",
    )
  return ent


async def _ensure_user_scan(db: AsyncSession, *, user_id: UUID, preview_id: UUID) -> None:
  """
  Ensure there is a UserScan row linking this user to this preview scan.
  Safe to call multiple times; it will only insert when missing.
  """
  result = await db.execute(
    select(UserScan).where(
      UserScan.user_id == user_id,
      UserScan.preview_id == preview_id,
    ).limit(1)
  )
  existing = result.scalar_one_or_none()
  if existing:
    return
  db.add(UserScan(user_id=user_id, preview_id=preview_id))
  await db.commit()


@router.post("", response_model=ScanTeaserOut)
async def create_scan(
  request: Request,
  payload: ScanCreate,
  db: AsyncSession = Depends(get_db),
  current_user: Optional[User] = Depends(get_optional_user),
) -> ScanTeaserOut:
  domain = normalize_domain(payload.company_name)
  ip = get_client_ip(request)

  await verify_turnstile(getattr(payload, "turnstile_token", None), ip)
  # Accept compare context from the analyzer request without breaking
  # existing scan creation behavior for standard scans.
  _compare_mode = (getattr(payload, "compare_mode", None) or "solo").strip().lower()
  _user_product_description = (getattr(payload, "user_product_description", None) or "").strip()
  if _compare_mode != "own" or not _user_product_description:
    _compare_mode = "solo"
    _user_product_description = ""
  await enforce_preview_limits(ip, domain)
  await enforce_monthly_api_budget(db)

  now = datetime.now(timezone.utc)
  result = await db.execute(select(PreviewScan).where(PreviewScan.domain == domain).limit(1))
  existing = result.scalar_one_or_none()

  def _preview_is_usable(preview: PreviewScan) -> bool:
    data = preview.preview_json or {}
    if not isinstance(data, dict):
      return False
    if bool(data.get("low_signal")):
      return False
    points = data.get("top_pain_points")
    if not isinstance(points, list) or not any(str(x).strip() for x in points):
      return False
    return int(data.get("unlock_reviews_count") or 0) >= 6

  if existing and existing.expires_at > now and _preview_is_usable(existing):
    preview = existing
  else:
    expires_at = now + timedelta(days=7)
    if existing:
      preview_json = await run_preview_scan(domain, db, scan_id=existing.id)
      existing.preview_json = preview_json
      existing.created_at = now
      existing.expires_at = expires_at
      preview = existing
    else:
      preview_json = await run_preview_scan(domain, db, scan_id=None)
      preview = PreviewScan(
        domain=domain,
        preview_json=preview_json,
        created_at=now,
        expires_at=expires_at,
      )
      db.add(preview)
    await db.commit()
    await db.refresh(preview)

  # Link this preview to the current user so it appears in their dashboard,
  # without affecting anonymous usage.
  if current_user:
    await _ensure_user_scan(db, user_id=current_user.id, preview_id=preview.id)

  return _build_teaser(preview, None)


@router.get("", response_model=List[ScanListItem])
async def list_scans(
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> List[ScanListItem]:
  """
  Return all scans this user has run, whether unlocked or still teaser-only.

  - Rows are keyed by preview scan ID so links point to /report?scan_id=<preview_id>.
  - is_unlocked is True when there is a fresh, substantive FullScan for this user+domain.
  """
  result = await db.execute(
    select(PreviewScan, FullScan)
    .join(UserScan, UserScan.preview_id == PreviewScan.id)
    .outerjoin(
      FullScan,
      (FullScan.domain == PreviewScan.domain) & (FullScan.user_id == current_user.id),
    )
    .where(UserScan.user_id == current_user.id)
    .order_by(desc(PreviewScan.created_at))
  )
  rows = result.all()

  items: List[ScanListItem] = []
  seen_preview_ids: set[UUID] = set()
  for preview, full_scan in rows:
    if preview.id in seen_preview_ids:
      continue
    seen_preview_ids.add(preview.id)

    is_unlocked = False
    if full_scan and _cached_full_is_fresh(full_scan) and _full_report_has_substance(full_scan):
      is_unlocked = True

    items.append(
      ScanListItem(
        scan_id=preview.id,
        company_name=preview.domain,
        created_at=preview.created_at,
        is_unlocked=is_unlocked,
      )
    )

  return items


@router.get("/{scan_id}/teaser", response_model=ScanTeaserOut)
async def get_scan_teaser(
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
) -> ScanTeaserOut:
  preview: Optional[PreviewScan] = await db.get(PreviewScan, scan_id)
  if not preview:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

  result = await db.execute(select(FullScan).where(FullScan.domain == preview.domain).order_by(desc(FullScan.created_at)).limit(1))
  full_scan = result.scalar_one_or_none()
  if full_scan and (not _cached_full_is_fresh(full_scan) or not _full_report_has_substance(full_scan)):
    full_scan = None
  return _build_teaser(preview, full_scan)


@router.get("/{scan_id}", response_model=ScanDetailOut)
async def get_scan(
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
) -> ScanDetailOut:
  preview: Optional[PreviewScan] = await db.get(PreviewScan, scan_id)
  if not preview:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

  result = await db.execute(select(FullScan).where(FullScan.domain == preview.domain).order_by(desc(FullScan.created_at)).limit(1))
  full_scan = result.scalar_one_or_none()
  if full_scan and (not _cached_full_is_fresh(full_scan) or not _full_report_has_substance(full_scan)):
    full_scan = None
  return _build_detail(preview, full_scan)


@router.delete("/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scan(
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> None:
  """
  Delete a scan from the user's dashboard.

  The scan_id is the PreviewScan ID used in /report?scan_id=..., not the FullScan ID.
  We:
  - Remove the UserScan row for this user+preview.
  - Remove any FullScan rows for this user+domain.
  - Leave the shared PreviewScan cache intact (it may be reused by other users).
  """
  preview: Optional[PreviewScan] = await db.get(PreviewScan, scan_id)
  if preview:
    # Ensure this preview is actually associated with the current user.
    result = await db.execute(
      select(UserScan).where(
        UserScan.preview_id == preview.id,
        UserScan.user_id == current_user.id,
      ).limit(1)
    )
    user_scan = result.scalar_one_or_none()
    if not user_scan:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

    # Delete any full reports this user has for this domain.
    full_result = await db.execute(
      select(FullScan).where(
        FullScan.user_id == current_user.id,
        FullScan.domain == preview.domain,
      )
    )
    full_scans = full_result.scalars().all()
    for fs in full_scans:
      await db.delete(fs)

    await db.delete(user_scan)
    await db.commit()
    return

  # Backward-compatibility: if scan_id was a FullScan ID, fall back to old behaviour.
  full_scan: Optional[FullScan] = await db.get(FullScan, scan_id)
  if full_scan:
    if full_scan.user_id != current_user.id:
      raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You can only delete your own scans.",
      )
    await db.delete(full_scan)
    await db.commit()
    return

  raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")


@router.post("/{scan_id}/checkout", response_model=CheckoutSessionOut)
async def create_checkout_session(
  request: Request,
  scan_id: UUID,
  payload: ScanCheckoutRequest | None = None,
  plan: Optional[str] = Query(None),
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> CheckoutSessionOut:
  preview: Optional[PreviewScan] = await db.get(PreviewScan, scan_id)
  if not preview:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")
  try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    # Use request origin to keep return URL on the same host/origin the user is on.
    base = str(request.base_url).rstrip("/")
    chosen_plan = (payload.plan if payload and payload.plan else plan) or "single"
    price_id, checkout_mode = _resolve_plan_to_price_and_mode(chosen_plan)

    success_url = f"{base}/report?paid=1&scan_id={scan_id}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/report?scan_id={scan_id}"
    metadata = {"plan": (chosen_plan or "single").lower(), "scan_id": str(scan_id), "domain": preview.domain}
    create_kw: dict = {
      "mode": checkout_mode,
      "line_items": [{"price": price_id, "quantity": 1}],
      "success_url": success_url,
      "cancel_url": cancel_url,
      "client_reference_id": str(scan_id),
      "metadata": metadata,
    }
    if current_user and getattr(current_user, "email", None):
      create_kw["customer_email"] = current_user.email
    session = stripe.checkout.Session.create(**create_kw)
    if not session.url:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe did not return a checkout URL.")
    return CheckoutSessionOut(url=session.url)
  except HTTPException:
    raise
  except Exception as e:
    logger.exception("Stripe checkout session create failed: %s", e)
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Checkout failed.") from e


@router.post("/{scan_id}/unlock", response_model=ScanDetailOut)
async def unlock_scan(
  request: Request,
  scan_id: UUID,
  session_id: Optional[str] = Query(None),
  payload: Optional[ScanUnlockContext] = None,
  db: AsyncSession = Depends(get_db),
  current_user: Optional[User] = Depends(get_optional_user),
) -> ScanDetailOut:
  preview: Optional[PreviewScan] = await db.get(PreviewScan, scan_id)
  if not preview:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

  compare_mode = ((payload.compare_mode if payload else None) or "solo").strip().lower()
  user_product_description = ((payload.user_product_description if payload else None) or "").strip()
  if compare_mode != "own" or not user_product_description:
    compare_mode = "solo"
    user_product_description = None

  # Path 1: Return from Stripe with session_id – verify payment and unlock without requiring login or credit.
  if session_id and settings.STRIPE_SECRET_KEY:
    try:
      import stripe
      stripe.api_key = settings.STRIPE_SECRET_KEY
      session = stripe.checkout.Session.retrieve(session_id, expand=["line_items"])
      if getattr(session, "payment_status", None) == "paid" and str(getattr(session, "client_reference_id", None)) == str(scan_id):
        customer_email = getattr(session, "customer_email", None)
        if getattr(session, "customer_details", None) and hasattr(session.customer_details, "email"):
          customer_email = session.customer_details.email or customer_email
        user = await get_or_create_user_by_email(db, customer_email) if customer_email else None
        user_id = user.id if user else None

        result = await db.execute(
          select(FullScan)
          .where(FullScan.domain == preview.domain)
          .order_by(desc(FullScan.created_at))
          .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing and _cached_full_is_fresh(existing) and _full_report_has_substance(existing):
          if user_id and existing.user_id != user_id:
            clone = FullScan(
              user_id=user_id,
              domain=existing.domain,
              full_report_json=existing.full_report_json,
            )
            db.add(clone)
            await db.commit()
            await db.refresh(clone)
            existing = clone

          if user_id:
            await _ensure_user_scan(db, user_id=user_id, preview_id=preview.id)

          return _build_detail(preview, existing)

        await enforce_full_global_daily_limit()
        await enforce_monthly_api_budget(db)
        full_report = await run_full_scan(
          preview.domain,
          preview.preview_json or {},
          db,
          scan_id=preview.id,
          base_url=str(request.base_url),
          compare_mode=compare_mode,
          user_product_description=user_product_description,
        )
        if not user_id:
          raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not identify customer from payment. Please contact support.")
        full_scan = FullScan(
          user_id=user_id,
          domain=preview.domain,
          full_report_json=full_report,
        )
        db.add(full_scan)
        await db.commit()
        await db.refresh(full_scan)
        await _ensure_user_scan(db, user_id=user_id, preview_id=preview.id)
        return _build_detail(preview, full_scan)
    except HTTPException:
      raise
    except Exception as e:
      logger.warning("Unlock session_id verification failed: %s", e)

  # Path 2: Unlock with account (credit or already-unlocked). Requires login and verified email.
  if not current_user:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to unlock, or use the link from your payment confirmation.")
  if not current_user.is_verified:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verify your email before unlocking full reports.")

  # Idempotency guard: if this user already has a valid full scan for this
  # domain, don't consume another credit on repeated unlock attempts.
  my_existing_result = await db.execute(
    select(FullScan)
    .where(
      FullScan.domain == preview.domain,
      FullScan.user_id == current_user.id,
    )
    .order_by(desc(FullScan.created_at))
    .limit(1)
  )
  my_existing = my_existing_result.scalar_one_or_none()
  if my_existing and _cached_full_is_fresh(my_existing) and _full_report_has_substance(my_existing):
    await _ensure_user_scan(db, user_id=current_user.id, preview_id=preview.id)
    return _build_detail(preview, my_existing)

  result = await db.execute(
    select(FullScan)
    .where(FullScan.domain == preview.domain)
    .order_by(desc(FullScan.created_at))
    .limit(1)
  )
  existing = result.scalar_one_or_none()
  # We only charge a credit when an unlock is actually needed and about to succeed.
  ent = await _get_entitlement_or_402(db, current_user.id)
  if existing and _cached_full_is_fresh(existing) and _full_report_has_substance(existing):
    if existing.user_id != current_user.id:
      clone = FullScan(user_id=current_user.id, domain=existing.domain, full_report_json=existing.full_report_json)
      db.add(clone)
      ent.credits_remaining = int(ent.credits_remaining or 0) - 1
      await db.commit()
      await db.refresh(clone)
      existing = clone
    else:
      # Same-user fresh full scan is already handled above; keep defensive no-charge path.
      await _ensure_user_scan(db, user_id=current_user.id, preview_id=preview.id)
      return _build_detail(preview, existing)
    await _ensure_user_scan(db, user_id=current_user.id, preview_id=preview.id)
    return _build_detail(preview, existing)

  await enforce_full_global_daily_limit()
  await enforce_monthly_api_budget(db)
  full_report = await run_full_scan(
    preview.domain,
    preview.preview_json or {},
    db,
    scan_id=preview.id,
    base_url=str(request.base_url),
    compare_mode=compare_mode,
    user_product_description=user_product_description,
  )

  full_scan = FullScan(
    user_id=current_user.id,
    domain=preview.domain,
    full_report_json=full_report,
  )
  db.add(full_scan)
  ent.credits_remaining = int(ent.credits_remaining or 0) - 1
  await db.commit()
  await db.refresh(full_scan)
  await _ensure_user_scan(db, user_id=current_user.id, preview_id=preview.id)
  return _build_detail(preview, full_scan)


@router.post("/{scan_id}/unlock-with-credit", response_model=ScanDetailOut)
async def unlock_with_credit(
  request: Request,
  scan_id: UUID,
  payload: Optional[ScanUnlockContext] = None,
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> ScanDetailOut:
  # Keep compatibility with existing frontend path.
  return await unlock_scan(
    request=request,
    scan_id=scan_id,
    session_id=None,
    payload=payload,
    db=db,
    current_user=current_user,
  )
