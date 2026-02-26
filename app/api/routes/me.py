"""Me / account endpoints: credits and profile."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.entitlement import Entitlement
from app.models.user import User

router = APIRouter(prefix="/me", tags=["me"])
logger = logging.getLogger(__name__)


class CreditsOut(BaseModel):
  credits_remaining: int
  subscription_active: bool
  monthly_quota: Optional[int] = None
  renewal_date: Optional[datetime] = None


@router.get("/credits", response_model=CreditsOut)
async def get_my_credits(
  db: AsyncSession = Depends(get_db),
  current_user: User = Depends(get_current_user),
) -> CreditsOut:
  """Return the current user's credit balance and subscription state."""
  result = await db.execute(
    select(Entitlement).where(Entitlement.user_id == current_user.id).limit(1)
  )
  ent = result.scalar_one_or_none()
  if not ent:
    return CreditsOut(
      credits_remaining=0,
      subscription_active=False,
      monthly_quota=None,
      renewal_date=None,
    )
  return CreditsOut(
    credits_remaining=ent.credits_remaining or 0,
    subscription_active=ent.subscription_active or False,
    monthly_quota=ent.monthly_quota,
    renewal_date=ent.subscription_renewal_date,
  )
