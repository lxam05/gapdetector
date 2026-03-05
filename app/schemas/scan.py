from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel


class ScanCreate(BaseModel):
  company_name: str
  turnstile_token: Optional[str] = None
  compare_mode: Optional[Literal["solo", "own"]] = None
  user_product_description: Optional[str] = None


class ScanUnlockContext(BaseModel):
  compare_mode: Optional[Literal["solo", "own"]] = None
  user_product_description: Optional[str] = None


class ScanListItem(BaseModel):
  """One row for the user's previous scans list."""
  scan_id: UUID
  company_name: str
  created_at: datetime
  is_unlocked: bool

  class Config:
    from_attributes = True


class ScanBaseOut(BaseModel):
  scan_id: UUID
  company_name: str
  summary: str
  sentiment_score: int
  opportunity_score: int
  negative_percent_estimate: int
  positive_percent_estimate: int
  key_complaints: List[str]
  top_strengths: List[str]
  recurring_feature_requests_hidden: int
  unlock_reviews_count: int
  locked: bool


class ScanTeaserOut(ScanBaseOut):
  premium_preview: Optional[str] = None


class ScanDetailOut(ScanBaseOut):
  premium_preview: Optional[str] = None
  premium_analysis: Optional[str] = None
  created_at: datetime
  full_dashboard: Optional[dict] = None  # same shape as /analyze response for renderDashboard

  class Config:
    from_attributes = True

