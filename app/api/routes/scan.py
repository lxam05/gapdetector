from typing import List, Optional
from uuid import UUID

import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.core.config import settings
from app.db.session import get_db
from app.models.entitlement import Entitlement
from app.models.scan import Scan
from app.models.user import User
from app.services.stripe_webhook import get_or_create_user_by_email
from app.schemas.scan import ScanCreate, ScanDetailOut, ScanListItem, ScanTeaserOut

router = APIRouter(prefix="/scan", tags=["scan"])
logger = logging.getLogger(__name__)


class CheckoutSessionOut(BaseModel):
  url: str

# Timeout for internal call to /analyze (real pipeline can take 60+ seconds).
ANALYZE_TIMEOUT = 120.0


class ScanCheckoutRequest(BaseModel):
  plan: Optional[str] = None


def _resolve_plan_to_price_and_mode(plan: str) -> tuple[str, str]:
  """
  Map a logical plan name to a Stripe Price ID and Checkout mode for scan-specific checkout.

  Supports both the new plan names and older internal ones:
    - "single"                -> PRICE_SINGLE_REPORT (or legacy STRIPE_PRICE_ID), mode=payment
    - "bundle" / "bundle5"    -> PRICE_BUNDLE_5, mode=payment
    - "subscription"/"monthly"-> PRICE_SUB_MONTHLY, mode=subscription
  """
  chosen = (plan or "single").lower()
  mode = "payment"

  if chosen in ("bundle", "bundle5"):
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


def _build_teaser(scan: Scan) -> ScanTeaserOut:
  key_complaints = scan.key_complaints or []
  missed_opportunities = scan.missed_opportunities or []
  if not isinstance(key_complaints, list):
    key_complaints = [str(key_complaints)]
  if not isinstance(missed_opportunities, list):
    missed_opportunities = [str(missed_opportunities)]

  preview = (scan.premium_analysis or "").strip()
  if preview:
    preview = preview[:260].rstrip()
    if len(scan.premium_analysis) > len(preview):
      preview = preview + "…"

  return ScanTeaserOut(
    scan_id=scan.id,
    company_name=scan.company_name,
    summary=scan.summary,
    biggest_weakness=scan.biggest_weakness,
    key_complaints=key_complaints[:3],
    missed_opportunities=missed_opportunities[:2],
    locked=not scan.is_unlocked,
    premium_preview=preview or None,
  )


def _build_detail(scan: Scan) -> ScanDetailOut:
  teaser = _build_teaser(scan)
  return ScanDetailOut(
    scan_id=teaser.scan_id,
    company_name=teaser.company_name,
    summary=teaser.summary,
    biggest_weakness=teaser.biggest_weakness,
    key_complaints=teaser.key_complaints,
    missed_opportunities=teaser.missed_opportunities,
    locked=teaser.locked,
    premium_preview=teaser.premium_preview,
    premium_analysis=scan.premium_analysis if scan.is_unlocked else None,
    created_at=scan.created_at,
    full_dashboard=scan.full_analysis if (scan.is_unlocked and getattr(scan, "full_analysis", None)) else None,
  )


def _mock_analysis(company_name: str) -> dict:
  name = company_name.strip() or "This competitor"
  summary = (
    f"{name} is a widely used SaaS product with strong adoption among modern teams, "
    "but user feedback points to clear areas where a focused competitor could win."
  )
  biggest_weakness = (
    f"The biggest weakness users mention about {name} is how overwhelming and complex it feels "
    "once real-world data and teams scale up."
  )
  key_complaints = [
    f"{name} becomes slow and cluttered as more workspaces, databases, and automations are added.",
    "Teams struggle to standardise how they use the tool, leading to messy, duplicated setups.",
    "Reporting and insights feel bolted-on rather than designed for operators who need quick answers.",
  ]
  missed_opportunities = [
    "Opinionated templates and workflows for specific verticals (e.g. agencies, PLG SaaS, communities).",
    "A calmer, more guided experience for non-power users who only need the 10% of features they actually use.",
  ]
  premium_analysis = (
    f"For a founder targeting {name}'s market, the wedge is to focus on depth over breadth. "
    "Instead of recreating every feature, narrow into one or two high-value workflows and make them "
    "dramatically faster, clearer, and easier to adopt.\n\n"
    "Strategically, this means:\n"
    "- Choosing an ICP where {name}'s flexibility is actually a burden rather than a strength.\n"
    "- Designing onboarding around a few golden paths that get teams to value in one day, not weeks.\n"
    "- Building opinionated reporting that answers the real business questions without configuration.\n\n"
    "If you can repeatedly turn 'confusing and powerful' into 'calm and focused' for a specific segment, "
    "you'll own a story {name} can't plausibly tell without breaking its current product."
  )
  return {
    "summary": summary,
    "biggest_weakness": biggest_weakness,
    "key_complaints": key_complaints,
    "missed_opportunities": missed_opportunities,
    "premium_analysis": premium_analysis,
  }


async def _run_real_analyzer(base_url: str, company_name: str) -> Optional[dict]:
  """Call the app's /analyze endpoint and return the full dashboard payload, or None on failure."""
  url = f"{base_url.rstrip('/')}/analyze"
  try:
    async with httpx.AsyncClient(timeout=ANALYZE_TIMEOUT) as client:
      r = await client.post(url, json={"query": company_name})
      r.raise_for_status()
      return r.json()
  except Exception:
    return None


@router.post("", response_model=ScanTeaserOut)
async def create_scan(
  request: Request,
  payload: ScanCreate,
  db: AsyncSession = Depends(get_db),
  current_user: Optional[User] = Depends(get_optional_user),
) -> ScanTeaserOut:
  """Create a scan. Requires auth and at least one credit; consumes one credit and unlocks the report."""
  company = (payload.company_name or "").strip()
  if not company:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="company_name must not be empty.")

  if not current_user:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Sign in to run a scan.",
    )

  result = await db.execute(
    select(Entitlement).where(Entitlement.user_id == current_user.id).limit(1)
  )
  ent = result.scalar_one_or_none()
  if not ent:
    raise HTTPException(
      status_code=status.HTTP_402_PAYMENT_REQUIRED,
      detail="No credits remaining. Purchase credits or a subscription to run reports.",
    )
  remaining = ent.credits_remaining or 0
  if remaining <= 0:
    raise HTTPException(
      status_code=status.HTTP_402_PAYMENT_REQUIRED,
      detail="No credits remaining. Purchase credits or a subscription to run reports.",
    )

  ent.credits_remaining = remaining - 1

  analysis = _mock_analysis(company)

  scan = Scan(
    company_name=company,
    summary=analysis["summary"],
    biggest_weakness=analysis["biggest_weakness"],
    key_complaints=analysis["key_complaints"],
    missed_opportunities=analysis["missed_opportunities"],
    premium_analysis=analysis["premium_analysis"],
    is_unlocked=True,
    credits_used=True,
    user_id=current_user.id,
  )
  db.add(scan)
  await db.commit()
  await db.refresh(scan)

  # Run real analyzer so unlocked view gets the same full report as the app page.
  base_url = str(request.base_url)
  full_data = await _run_real_analyzer(base_url, company)
  if full_data:
    scan.premium_analysis = full_data.get("report") or scan.premium_analysis
    scan.full_analysis = full_data
    await db.commit()
    await db.refresh(scan)

  return _build_teaser(scan)


@router.get("", response_model=List[ScanListItem])
async def list_scans(
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> List[ScanListItem]:
  """Return the current user's scans, newest first. Requires auth."""
  stmt = (
    select(Scan)
    .where(Scan.user_id == current_user.id)
    .order_by(Scan.created_at.desc())
  )
  result = await db.execute(stmt)
  scans = result.scalars().all()
  return [
    ScanListItem(
      scan_id=s.id,
      company_name=s.company_name,
      created_at=s.created_at,
      is_unlocked=s.is_unlocked,
    )
    for s in scans
  ]


@router.get("/{scan_id}/teaser", response_model=ScanTeaserOut)
async def get_scan_teaser(
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
) -> ScanTeaserOut:
  """Return teaser only (for locked report page)."""
  scan: Optional[Scan] = await db.get(Scan, scan_id)
  if not scan:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")
  return _build_teaser(scan)


@router.get("/{scan_id}", response_model=ScanDetailOut)
async def get_scan(
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
) -> ScanDetailOut:
  scan: Optional[Scan] = await db.get(Scan, scan_id)
  if not scan:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")
  return _build_detail(scan)


@router.post("/{scan_id}/checkout", response_model=CheckoutSessionOut)
async def create_checkout_session(
  scan_id: UUID,
  payload: ScanCheckoutRequest | None = None,
  plan: Optional[str] = Query(None),
  db: AsyncSession = Depends(get_db),
  current_user: Optional[User] = Depends(get_optional_user),
) -> CheckoutSessionOut:
  """Create a Stripe Checkout Session so success_url includes scan_id. No localStorage needed."""
  scan: Optional[Scan] = await db.get(Scan, scan_id)
  if not scan:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")
  try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    base = settings.SUCCESS_URL_BASE.rstrip("/")
    chosen_plan = (payload.plan if payload and payload.plan else plan) or "single"
    price_id, checkout_mode = _resolve_plan_to_price_and_mode(chosen_plan)

    success_url = f"{base}/report?paid=1&scan_id={scan_id}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/report?scan_id={scan_id}"
    metadata = {
      "plan": (chosen_plan or "single").lower(),
      "scan_id": str(scan_id),
    }
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
    err_msg = str(e).strip() if str(e) else "Unknown error"
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail=f"Checkout failed: {err_msg}. Check STRIPE_SECRET_KEY and STRIPE_PRICE_ID (use test keys and a Price ID from the same Stripe account).",
    ) from e


@router.post("/{scan_id}/unlock", response_model=ScanDetailOut)
async def unlock_scan(
  request: Request,
  scan_id: UUID,
  session_id: Optional[str] = None,
  paid: Optional[int] = None,
  db: AsyncSession = Depends(get_db),
  current_user: Optional[User] = Depends(get_optional_user),
) -> ScanDetailOut:
  """
  Unlock a scan. Only via (1) Stripe session_id verification or (2) authenticated user owning the scan.
  Do not trust paid=1; use session_id from success URL and verify with Stripe.
  """
  scan: Optional[Scan] = await db.get(Scan, scan_id)
  if not scan:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

  if session_id and settings.STRIPE_SECRET_KEY:
    try:
      import stripe
      stripe.api_key = settings.STRIPE_SECRET_KEY
      session = stripe.checkout.Session.retrieve(session_id, expand=["line_items"])
      if getattr(session, "payment_status", None) == "paid" and str(getattr(session, "client_reference_id", None)) == str(scan_id):
        scan.is_unlocked = True
        if scan.user_id is None and getattr(session, "customer_email", None):
          user = await get_or_create_user_by_email(db, session.customer_email)
          if user:
            scan.user_id = user.id
        await db.commit()
        await db.refresh(scan)
        return _build_detail(scan)
    except Exception as e:
      logger.warning("Unlock session_id verification failed: %s", e)

  if current_user and scan.user_id == current_user.id:
    scan.is_unlocked = True
  else:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unlock not authorized. Use the link from your payment confirmation.")

  if getattr(scan, "full_analysis", None) is None:
    base_url = str(request.base_url)
    full_data = await _run_real_analyzer(base_url, scan.company_name)
    if full_data:
      scan.premium_analysis = full_data.get("report") or scan.premium_analysis
      scan.full_analysis = full_data

  await db.commit()
  await db.refresh(scan)
  return _build_detail(scan)


@router.post("/{scan_id}/unlock-with-credit", response_model=ScanDetailOut)
async def unlock_with_credit(
  request: Request,
  scan_id: UUID,
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> ScanDetailOut:
  """
  Unlock a scan by consuming one credit from the current user's entitlement.
  If no credits are available, returns 402 so the UI can route to pricing.
  """
  scan: Optional[Scan] = await db.get(Scan, scan_id)
  if not scan:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found.")

  result = await db.execute(
    select(Entitlement).where(Entitlement.user_id == current_user.id).limit(1)
  )
  ent = result.scalar_one_or_none()
  remaining = ent.credits_remaining if ent and ent.credits_remaining is not None else 0
  if not ent or remaining <= 0:
    raise HTTPException(
      status_code=status.HTTP_402_PAYMENT_REQUIRED,
      detail="No credits remaining. Purchase credits or a subscription to unlock reports.",
    )

  ent.credits_remaining = remaining - 1
  scan.is_unlocked = True
  if hasattr(scan, "credits_used"):
    scan.credits_used = True
  if scan.user_id is None:
    scan.user_id = current_user.id

  if getattr(scan, "full_analysis", None) is None:
    base_url = str(request.base_url)
    full_data = await _run_real_analyzer(base_url, scan.company_name)
    if full_data:
      scan.premium_analysis = full_data.get("report") or scan.premium_analysis
      scan.full_analysis = full_data

  await db.commit()
  await db.refresh(scan)
  return _build_detail(scan)

