import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from google_play_scraper import Sort, reviews  # type: ignore[import]


logger = logging.getLogger(__name__)


# Configuration constants (can be overridden by importing code if needed).
PLAYSTORE_ENABLED: bool = True
PLAYSTORE_CACHE_HOURS: int = 24
PLAYSTORE_TARGET_REVIEWS: int = 150
PLAYSTORE_LOW_RATING_RATIO: float = 0.8


@dataclass
class PlayStoreReview:
  """Lightweight representation of a single Play Store review."""

  text: str
  rating: int
  date: Optional[datetime]
  app_version: Optional[str]
  thumbs_up_count: Optional[int]
  is_update_related: bool


_play_store_cache: Dict[str, Tuple[datetime, List[PlayStoreReview]]] = {}


def _is_probably_english(text: str) -> bool:
  """Very lightweight language heuristic to keep English-ish reviews."""
  if not text:
    return False
  ascii_letters = sum(1 for ch in text if "a" <= ch.lower() <= "z" or ch == " ")
  ratio = ascii_letters / max(len(text), 1)
  if ratio < 0.6:
    return False
  lowered = text.lower()
  common_tokens = (" the ", " and ", " you ", " for ", " with ", " app ")
  return any(tok in lowered for tok in common_tokens)


def _clean_text(text: str) -> str:
  """Normalize whitespace and strip basic noise for filtering."""
  return " ".join(text.split()).strip()


def _is_junk_review(text: str) -> bool:
  """Filter out very short, generic, or spammy reviews."""
  if not text:
    return True
  if len(text) < 15:
    return True

  lowered = text.lower()
  generic_phrases = [
    "good app",
    "nice app",
    "great app",
    "very good",
    "nice",
    "good",
    "love it",
    "awesome app",
  ]
  if lowered in generic_phrases:
    return True

  # Emoji-heavy: if more than 70% of chars are non-basic ASCII, treat as junk.
  non_basic = sum(1 for ch in text if ord(ch) > 127)
  if non_basic / max(len(text), 1) > 0.7:
    return True

  return False


def _mark_update_related(text: str) -> bool:
  lowered = text.lower()
  keywords = [
    "update",
    "latest version",
    "since update",
    "after update",
    "new version",
    "update broke",
    "crashing now",
  ]
  return any(k in lowered for k in keywords)


def _dedupe_reviews(items: List[PlayStoreReview]) -> List[PlayStoreReview]:
  seen: Dict[Tuple[str, int], PlayStoreReview] = {}
  for r in items:
    key = (r.text, r.rating)
    if key not in seen:
      seen[key] = r
  return list(seen.values())


def _fetch_reviews_sync(package_name: str) -> List[PlayStoreReview]:
  """Synchronous worker that talks to google-play-scraper."""
  if not PLAYSTORE_ENABLED:
    return []

  # Check cache
  now = datetime.now(timezone.utc)
  cached = _play_store_cache.get(package_name)
  if cached:
    ts, cached_reviews = cached
    if now - ts < timedelta(hours=PLAYSTORE_CACHE_HOURS):
      return cached_reviews

  target = PLAYSTORE_TARGET_REVIEWS
  low_ratio = PLAYSTORE_LOW_RATING_RATIO
  low_target = int(target * low_ratio)
  high_target = target - low_target

  collected: List[PlayStoreReview] = []

  def _convert(raw: Dict[str, Any]) -> Optional[PlayStoreReview]:
    text = _clean_text(str(raw.get("content") or ""))
    if not text:
      return None
    if not _is_probably_english(text):
      return None
    if _is_junk_review(text):
      return None
    rating = int(raw.get("score") or 0)
    at = raw.get("at")
    date_val: Optional[datetime] = at if isinstance(at, datetime) else None
    app_version = raw.get("version") if raw.get("version") else None
    thumbs = raw.get("thumbsUpCount")
    is_update = _mark_update_related(text)
    return PlayStoreReview(
      text=text,
      rating=rating,
      date=date_val,
      app_version=app_version,
      thumbs_up_count=int(thumbs) if isinstance(thumbs, int) else None,
      is_update_related=is_update,
    )

  try:
    # Low ratings first (1–2 stars)
    low_reviews: List[PlayStoreReview] = []
    for score_filter in (1, 2):
      if len(low_reviews) >= low_target:
        break
      raw, _ = reviews(
        package_name,
        lang="en",
        country="us",
        sort=Sort.NEWEST,
        count=low_target,
        filter_score_with=[score_filter],
      )
      for r in raw:
        conv = _convert(r)
        if conv:
          low_reviews.append(conv)
          if len(low_reviews) >= low_target:
            break

    # High ratings for balance (4–5 stars)
    high_reviews: List[PlayStoreReview] = []
    for score_filter in (5, 4):
      if len(high_reviews) >= high_target:
        break
      raw, _ = reviews(
        package_name,
        lang="en",
        country="us",
        sort=Sort.NEWEST,
        count=high_target,
        filter_score_with=[score_filter],
      )
      for r in raw:
        conv = _convert(r)
        if conv:
          high_reviews.append(conv)
          if len(high_reviews) >= high_target:
            break

    collected = _dedupe_reviews(low_reviews + high_reviews)
  except Exception as exc:  # pragma: no cover - defensive
    logger.warning("Failed to fetch Play Store reviews for %s: %s", package_name, exc)
    collected = []

  _play_store_cache[package_name] = (now, collected)
  return collected


async def fetch_play_store_reviews(package_name: str) -> List[PlayStoreReview]:
  """
  Async wrapper around google-play-scraper.reviews().

  Returns a list of processed, filtered PlayStoreReview objects.
  """
  return await asyncio.to_thread(_fetch_reviews_sync, package_name)

