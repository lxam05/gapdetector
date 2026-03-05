from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.api_usage_log import ApiUsageLog

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
JUNK_TLDS = {"local", "internal", "test", "example", "invalid"}


def normalize_domain(raw: str) -> str:
  value = (raw or "").strip().lower()
  value = re.sub(r"^https?://", "", value)
  value = re.sub(r"^www\.", "", value)
  value = value.split("/")[0].split("?")[0].split("#")[0].strip()

  if not value or len(value) < 2:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain.")
  if value in {"localhost", "127.0.0.1"}:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="localhost is not allowed.")
  if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="IP addresses are not allowed.")
  if "_" in value or ".." in value:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain format.")
  # Allow product/company names without TLD: treat as domain with .com (e.g. "Notion" -> "notion.com")
  if "." not in value and re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", value):
    value = value + ".com"
  if not DOMAIN_RE.match(value):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter a domain or company name (e.g. stripe.com or Notion).")
  if value.split(".")[-1] in JUNK_TLDS:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain.")
  return value


def get_client_ip(request: Request) -> str:
  xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
  if xff:
    return xff
  if request.client and request.client.host:
    return request.client.host
  return "unknown"


@lru_cache()
def _redis_client() -> Optional[Redis]:
  if not settings.REDIS_URL:
    return None
  return Redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)


async def verify_turnstile(token: Optional[str], remote_ip: str) -> None:
  """Verify Cloudflare Turnstile token. No-op if Turnstile is not configured."""
  if not settings.TURNSTILE_SECRET_KEY:
    return
  if not (token and token.strip()):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Turnstile token.")

  async with httpx.AsyncClient(timeout=8.0) as client:
    resp = await client.post(
      "https://challenges.cloudflare.com/turnstile/v0/siteverify",
      data={
        "secret": settings.TURNSTILE_SECRET_KEY,
        "response": token,
        "remoteip": remote_ip,
      },
    )
    data = resp.json()
    if not data.get("success"):
      raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Turnstile verification failed.")


async def enforce_preview_limits(ip: str, domain: str) -> None:
  redis_client = _redis_client()
  if redis_client is None:
    return  # No Redis: skip rate limits (e.g. local dev). Set REDIS_URL in production.

  now = datetime.now(timezone.utc)
  day_key = f"rl:preview:ip_day:{ip}:{now.strftime('%Y-%m-%d')}"
  burst_key = f"rl:preview:ip_5m:{ip}"
  repeat_key = f"rl:preview:ip_domain:{ip}:{domain}"
  global_key = f"rl:preview:global_day:{now.strftime('%Y-%m-%d')}"

  day_count = await redis_client.incr(day_key)
  if day_count == 1:
    await redis_client.expire(day_key, 24 * 60 * 60)
  if day_count > settings.PREVIEW_IP_PER_DAY_LIMIT:
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Preview limit reached for this IP (24h).")

  burst_count = await redis_client.incr(burst_key)
  if burst_count == 1:
    await redis_client.expire(burst_key, 5 * 60)
  if burst_count > settings.PREVIEW_IP_PER_5M_LIMIT:
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Please wait before running another preview.")

  set_ok = await redis_client.set(repeat_key, "1", ex=settings.PREVIEW_DOMAIN_REPEAT_BLOCK_SECONDS, nx=True)
  if not set_ok:
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="You already scanned this domain recently.")

  global_count = await redis_client.incr(global_key)
  if global_count == 1:
    await redis_client.expire(global_key, 24 * 60 * 60)
  if global_count > settings.PREVIEW_GLOBAL_DAILY_LIMIT:
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Daily preview capacity reached. Try again tomorrow.")


async def enforce_full_global_daily_limit() -> None:
  redis_client = _redis_client()
  if redis_client is None:
    return  # No Redis: skip global limit (e.g. local dev).

  now = datetime.now(timezone.utc)
  key = f"rl:full:global_day:{now.strftime('%Y-%m-%d')}"
  count = await redis_client.incr(key)
  if count == 1:
    await redis_client.expire(key, 24 * 60 * 60)
  if count > settings.FULL_GLOBAL_DAILY_LIMIT:
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Daily full-scan capacity reached.")


async def enforce_monthly_api_budget(db: AsyncSession) -> None:
  now = datetime.now(timezone.utc)
  month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
  result = await db.execute(
    select(func.count(ApiUsageLog.id)).where(ApiUsageLog.created_at >= month_start)
  )
  call_count = int(result.scalar_one() or 0)
  if call_count >= settings.MAX_MONTHLY_API_CALLS:
    raise HTTPException(
      status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
      detail="API usage threshold reached. Please try again later.",
    )


async def log_api_usage(
  db: AsyncSession,
  *,
  scan_kind: str,
  scan_id,
  domain: str,
  provider: str,
  operation: str,
  input_tokens: Optional[int] = None,
  output_tokens: Optional[int] = None,
  cost_usd: Optional[float] = None,
) -> None:
  db.add(
    ApiUsageLog(
      scan_kind=scan_kind,
      scan_id=scan_id,
      domain=domain,
      provider=provider,
      operation=operation,
      input_tokens=input_tokens,
      output_tokens=output_tokens,
      cost_usd=cost_usd,
    )
  )
  await db.commit()
