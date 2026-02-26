from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel


class ScanCreate(BaseModel):
  company_name: str


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
  biggest_weakness: str
  key_complaints: List[str]
  missed_opportunities: List[str]
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

