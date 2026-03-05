from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.preview_scan import PreviewScan
from app.services.scan_guardrails import log_api_usage


def _sentence_snippets(text: str, limit: int) -> List[str]:
  if not text:
    return []
  parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
  out: List[str] = []
  for part in parts:
    p = part.strip()
    if len(p) < 25:
      continue
    if len(p) > 220:
      p = p[:219].rstrip() + "…"
    out.append(p)
    if len(out) >= limit:
      break
  return out


def _extract_text(html: str, max_chars: int) -> str:
  text = trafilatura.extract(html, include_comments=False, include_tables=False, no_fallback=False) or ""
  return " ".join(text.split())[:max_chars]


async def _serp_query(query: str, *, max_urls: int) -> Dict[str, List[str]]:
  if not settings.SERPER_API_KEY:
    return {"urls": [], "snippets": []}
  payload = {"q": query, "num": max(10, max_urls)}
  headers = {"X-API-KEY": settings.SERPER_API_KEY, "Content-Type": "application/json"}
  try:
    async with httpx.AsyncClient(timeout=10.0) as client:
      res = await client.post("https://google.serper.dev/search", json=payload, headers=headers)
      res.raise_for_status()
      data = res.json()
  except httpx.HTTPError:
    # On any Serper error (including 400s), degrade gracefully by returning no URLs
    return {"urls": [], "snippets": []}
  urls: List[str] = []
  snippets: List[str] = []
  for row in data.get("organic", []):
    link = (row.get("link") or "").strip()
    if link and link not in urls:
      urls.append(link)
    snippet = " ".join(str(row.get("snippet") or "").split()).strip()
    if snippet and snippet not in snippets:
      snippets.append(snippet[:240])
    if len(urls) >= max_urls:
      break
  return {"urls": urls, "snippets": snippets}


async def _fetch_url_text(url: str) -> str:
  try:
    async with httpx.AsyncClient(timeout=12.0) as client:
      res = await client.get(url, follow_redirects=True)
      res.raise_for_status()
      if "text/html" not in (res.headers.get("content-type") or "").lower():
        return ""
      return _extract_text(res.text, max_chars=2500)
  except Exception:
    return ""


async def _collect_snippets(urls: List[str], *, max_total_snippets: int, per_url_limit: int) -> List[str]:
  tasks = [_fetch_url_text(url) for url in urls]
  pages = await asyncio.gather(*tasks, return_exceptions=True)
  snippets: List[str] = []
  for page in pages:
    if isinstance(page, Exception) or not page:
      continue
    snippets.extend(_sentence_snippets(page, per_url_limit))
    if len(snippets) >= max_total_snippets:
      break
  return snippets[:max_total_snippets]


def _strip_tags(html: str) -> str:
  if not html:
    return ""
  without_scripts = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
  without_styles = re.sub(r"<style[\s\S]*?</style>", " ", without_scripts, flags=re.IGNORECASE)
  text = re.sub(r"<[^>]+>", " ", without_styles)
  return " ".join(text.split())


def _extract_first_tag_text(html: str, tag: str) -> str:
  m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html, flags=re.IGNORECASE)
  if not m:
    return ""
  return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())


def _extract_meta_description(html: str) -> str:
  m = re.search(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    html,
    flags=re.IGNORECASE,
  )
  return " ".join((m.group(1) if m else "").split())


def _extract_anchor_hrefs(html: str, base_url: str) -> List[str]:
  hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
  out: List[str] = []
  base_host = (urlparse(base_url).hostname or "").lower()
  for href in hrefs:
    h = href.strip()
    if not h or h.startswith("#") or h.startswith("mailto:") or h.startswith("tel:"):
      continue
    full = urljoin(base_url, h)
    host = (urlparse(full).hostname or "").lower()
    if base_host and host and host != base_host:
      continue
    if full not in out:
      out.append(full)
  return out


def _pick_link(links: List[str], keywords: List[str]) -> Optional[str]:
  for link in links:
    low = link.lower()
    if any(k in low for k in keywords):
      return link
  return None


async def _fetch_html(url: str) -> Dict[str, Any]:
  try:
    async with httpx.AsyncClient(timeout=12.0) as client:
      res = await client.get(url, follow_redirects=True)
      if "text/html" not in (res.headers.get("content-type") or "").lower():
        return {}
      return {"url": str(res.url), "html": res.text}
  except Exception:
    return {}


def _extract_lines_with_keywords(text: str, keywords: List[str], *, limit: int) -> List[str]:
  if not text:
    return []
  out: List[str] = []
  for line in re.split(r"(?<=[.!?])\s+", text):
    cleaned = " ".join(line.split()).strip()
    if len(cleaned) < 20:
      continue
    low = cleaned.lower()
    if any(k in low for k in keywords):
      out.append(cleaned[:220])
    if len(out) >= limit:
      break
  return out


def _extract_website_copy_from_pages(pages: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
  homepage_html = str(((pages.get("homepage") or {}).get("html")) or "")
  pricing_html = str(((pages.get("pricing") or {}).get("html")) or "")
  faq_html = str(((pages.get("faq") or {}).get("html")) or "")

  homepage_text = _extract_text(homepage_html, max_chars=9000) if homepage_html else ""
  pricing_text = _extract_text(pricing_html, max_chars=5000) if pricing_html else ""
  faq_text = _extract_text(faq_html, max_chars=5000) if faq_html else ""
  merged_text = " ".join(part for part in [homepage_text, pricing_text, faq_text] if part)

  headline = _extract_first_tag_text(homepage_html, "h1") or _extract_first_tag_text(homepage_html, "title")
  subheadline = _extract_meta_description(homepage_html)
  hero_statement = " ".join(merged_text.split()[:48])
  feature_sections = _extract_lines_with_keywords(
    merged_text,
    ["feature", "capability", "automation", "integration", "workflow", "dashboard", "analytics"],
    limit=12,
  )
  pricing_content = pricing_text or "Pricing page not found."
  faq_copy = _extract_lines_with_keywords(faq_text or merged_text, ["?", "faq", "question"], limit=10)

  return {
    "headline": headline[:240],
    "subheadline": subheadline[:300],
    "hero_positioning_statement": hero_statement[:420],
    "feature_sections": feature_sections,
    "pricing_page_content": pricing_content[:2500],
    "faq_copy": faq_copy,
    "raw_website_excerpt": merged_text[:5000],
  }


def _domain_seed(domain: str) -> str:
  host = (domain or "").strip().lower()
  if host.startswith("www."):
    host = host[4:]
  parts = [p for p in host.split(".") if p]
  if not parts:
    return host
  # Keep only the first label for search intent.
  return parts[0]


def _looks_like_mobile_app_target(domain: str, website_copy: Dict[str, Any]) -> bool:
  # Fast cheap gating: only run Play discovery for likely app-first companies.
  evidence = " ".join(
    str(website_copy.get(k) or "")
    for k in ("headline", "subheadline", "hero_positioning_statement", "raw_website_excerpt")
  ).lower()
  seed = _domain_seed(domain)

  strong_markers = (
    "download on the app store",
    "get it on google play",
    "app store",
    "google play",
    "ios app",
    "android app",
    "mobile app",
    "download the app",
  )
  if any(marker in evidence for marker in strong_markers):
    return True

  # If there is no explicit mobile signal, default to skip to save requests.
  web_only_markers = (
    "web app",
    "browser-based",
    "in your browser",
    "desktop app",
    "b2b saas",
  )
  if any(marker in evidence for marker in web_only_markers):
    return False

  # Fallback: only a small set of obvious app brands should pass without explicit copy.
  obvious_app_brands = {"nike", "spotify", "uber", "airbnb", "tiktok", "instagram", "snapchat", "duolingo"}
  return seed in obvious_app_brands


def _extract_play_package_id(url: str) -> Optional[str]:
  if "play.google.com/store/apps/details" not in (url or ""):
    return None
  parsed = urlparse(url)
  query = parsed.query or ""
  for part in query.split("&"):
    if part.startswith("id="):
      pkg = part.split("=", 1)[1].strip()
      if pkg:
        return pkg
  return None


async def _discover_play_store_package(domain: str, website_copy: Dict[str, Any]) -> Optional[str]:
  if not settings.SERPER_API_KEY:
    return None
  if not _looks_like_mobile_app_target(domain, website_copy):
    return None

  seed = _domain_seed(domain)
  query = f"\"{seed}\" app site:play.google.com/store/apps/details"
  result = await _serp_query(query, max_urls=8)
  for url in (result.get("urls") or []):
    pkg = _extract_play_package_id(url)
    if pkg:
      return pkg
  return None


async def _collect_play_store_snippets(package_name: str) -> Dict[str, Any]:
  try:
    from playstore import fetch_play_store_reviews
  except Exception:
    # Dependency unavailable or module import failure -> skip gracefully.
    return {"snippets": [], "source_url": None}

  try:
    reviews = await fetch_play_store_reviews(package_name)
  except Exception:
    return {"snippets": [], "source_url": None}

  snippets: List[str] = []
  for review in reviews[:80]:
    text = " ".join(str(getattr(review, "text", "") or "").split()).strip()
    if not text:
      continue
    rating = int(getattr(review, "rating", 0) or 0)
    update_related = bool(getattr(review, "is_update_related", False))
    label = f"Play Store ({rating}★"
    if update_related:
      label += ", update-related"
    label += f"): {text}"
    snippets.append(label[:260])
  return {
    "snippets": snippets[:48],
    "source_url": f"https://play.google.com/store/apps/details?id={package_name}",
  }


def _safe_json_load(raw: str) -> Dict[str, Any]:
  if not raw or not raw.strip():
    return {}
  text = raw.strip()
  # Remove markdown fences if present.
  text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
  text = re.sub(r"\s*```$", "", text)
  try:
    data = json.loads(text)
    return data if isinstance(data, dict) else {}
  except Exception:
    # Best-effort: extract first JSON object from mixed prose output.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
      return {}
    try:
      data = json.loads(match.group(0))
      return data if isinstance(data, dict) else {}
    except Exception:
      return {}


async def _openai_json(prompt: str, *, max_tokens: int = 700, system_prompt: str = "Return strict JSON only.") -> Dict[str, Any]:
  """
  JSON helper backed by Anthropic (despite the legacy name).

  Uses ANTHROPIC_API_KEY / ANTHROPIC_MODEL from settings and enforces that a model
  call actually happens; if misconfigured, it raises instead of silently returning {}.
  """
  from anthropic import AsyncAnthropic  # Imported lazily to avoid circular imports

  if not settings.ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY is required for scan analysis but is not set.")

  model_name = settings.ANTHROPIC_MODEL or "claude-sonnet-4-20250514"
  client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
  response = await client.messages.create(
    model=model_name,
    max_tokens=max_tokens,
    temperature=0.2,
    system=system_prompt,
    messages=[{"role": "user", "content": prompt}],
  )

  # Anthropic responses contain a list of content blocks; join text segments
  parts: List[str] = []
  for block in getattr(response, "content", []) or []:
    text = getattr(block, "text", None)
    if not text and isinstance(block, dict):
      text = block.get("text")
    if text:
      parts.append(text)

  content = "\n".join(parts).strip()
  if not content:
    raise RuntimeError("Empty response from Anthropic when JSON was expected.")

  parsed = _safe_json_load(content)
  if parsed:
    return parsed

  # Recovery pass: ask the model to convert its own output into strict JSON only.
  retry = await client.messages.create(
    model=model_name,
    max_tokens=max_tokens,
    temperature=0.0,
    system="Convert the user text into one strict JSON object only. No prose. No markdown.",
    messages=[{"role": "user", "content": content}],
  )
  retry_parts: List[str] = []
  for block in getattr(retry, "content", []) or []:
    text = getattr(block, "text", None)
    if not text and isinstance(block, dict):
      text = block.get("text")
    if text:
      retry_parts.append(text)
  return _safe_json_load("\n".join(retry_parts).strip())


def _int_0_100(value: Any, default: int) -> int:
  try:
    v = int(float(value))
    return max(0, min(100, v))
  except Exception:
    return default


def _list_of_strings(value: Any, *, limit: int) -> List[str]:
  if not isinstance(value, list):
    return []
  out: List[str] = []
  for item in value:
    text = str(item).strip()
    if text:
      out.append(text)
    if len(out) >= limit:
      break
  return out


def _is_placeholder_text(value: Any) -> bool:
  text = str(value or "").strip().lower()
  if not text:
    return True
  bad_markers = [
    "unable to determine",
    "not available",
    "unknown",
    "n/a",
    "access denied",
    "website access denied",
    "no data",
    "not found",
  ]
  return any(marker in text for marker in bad_markers)


def _clean_signal_list(value: Any, *, limit: int) -> List[str]:
  out: List[str] = []
  for item in _list_of_strings(value, limit=limit * 2):
    if _is_placeholder_text(item):
      continue
    if item not in out:
      out.append(item)
    if len(out) >= limit:
      break
  return out


def _normalize_better_than_you_points(value: Any, *, min_items: int = 5, max_items: int = 10) -> List[str]:
  points = _clean_signal_list(value, limit=max_items)
  if len(points) < min_items:
    return []
  return points[:max_items]


LOW_SIGNAL_MESSAGE = "Insufficient review density to extract statistically meaningful complaint clusters."
SERP_CACHE_TTL = timedelta(hours=48)
WEBSITE_CACHE_TTL = timedelta(hours=48)
MIN_SNIPPETS_FOR_COMPLAINTS = 6


def _utcnow() -> datetime:
  return datetime.now(timezone.utc)


def _iso_now() -> str:
  return _utcnow().isoformat()


def _parse_iso_dt(value: Any) -> Optional[datetime]:
  if not isinstance(value, str) or not value.strip():
    return None
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
      parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
  except Exception:
    return None


def _cache_is_fresh(cached_at: Any, ttl: timedelta) -> bool:
  dt = _parse_iso_dt(cached_at)
  if not dt:
    return False
  return dt >= (_utcnow() - ttl)


def _normalize_snippets(value: Any, *, max_items: int) -> List[str]:
  out: List[str] = []
  if not isinstance(value, list):
    return out
  for item in value:
    text = str(item).strip()
    if text:
      out.append(text)
    if len(out) >= max_items:
      break
  return out


def _normalize_urls(value: Any, *, max_items: int) -> List[str]:
  out: List[str] = []
  if not isinstance(value, list):
    return out
  for item in value:
    text = str(item).strip()
    if text and text not in out:
      out.append(text)
    if len(out) >= max_items:
      break
  return out


def _extract_serp_cache(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
  if not isinstance(payload, dict):
    return None
  cache = payload.get("serp_cache")
  if not isinstance(cache, dict):
    return None
  if not _cache_is_fresh(cache.get("cached_at"), SERP_CACHE_TTL):
    return None
  snippets = _normalize_snippets(cache.get("snippets"), max_items=64)
  urls = _normalize_urls(cache.get("urls"), max_items=8)
  # Only reuse cached SERP evidence when there is enough signal to be useful.
  # Tiny caches often produce generic fallback reports.
  if len(snippets) < 8:
    return None
  return {
    "query": str(cache.get("query") or "").strip(),
    "urls": urls,
    "snippets": snippets,
    "cached_at": str(cache.get("cached_at") or ""),
    "cache_hit": True,
  }


def _extract_website_cache(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
  if not isinstance(payload, dict):
    return None
  cache = payload.get("website_cache")
  if not isinstance(cache, dict):
    return None
  if not _cache_is_fresh(cache.get("cached_at"), WEBSITE_CACHE_TTL):
    return None
  copy = cache.get("website_copy")
  if not isinstance(copy, dict):
    return None
  return {
    "website_copy": copy,
    "cached_at": str(cache.get("cached_at") or ""),
    "cache_hit": True,
  }


async def _load_domain_cache(domain: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
  result = await db.execute(select(PreviewScan).where(PreviewScan.domain == domain).limit(1))
  row = result.scalar_one_or_none()
  if not row:
    return None
  return _extract_serp_cache(row.preview_json or {})


def _build_serp_cache(query: str, urls: List[str], snippets: List[str]) -> Dict[str, Any]:
  return {
    "query": query,
    "urls": urls[:8],
    "snippets": snippets[:64],
    "cached_at": _iso_now(),
  }


def _build_website_cache(website_copy: Dict[str, Any]) -> Dict[str, Any]:
  return {
    "cached_at": _iso_now(),
    "website_copy": website_copy,
  }


async def _get_domain_snippets(
  *,
  domain: str,
  db: AsyncSession,
  scan_kind: str,
  scan_id=None,
  preview_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
  cached_from_preview = _extract_serp_cache(preview_data or {})
  if cached_from_preview:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="cache", operation="reuse_snippets_48h_preview_payload")
    return cached_from_preview

  cached_from_db = await _load_domain_cache(domain, db)
  if cached_from_db:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="cache", operation="reuse_snippets_48h_domain_cache")
    return cached_from_db

  # Fan out across a few focused Serper queries to improve signal while
  # keeping total API usage low.
  queries = [
    f"\"{domain}\" reviews -site:{domain}",
    f"\"{domain}\" complaints -site:{domain}",
    f"\"{domain}\" problems -site:{domain}",
    f"{domain} site:trustpilot.com",
    f"{domain} site:g2.com",
    f"{domain} site:capterra.com",
  ]
  urls: List[str] = []
  seed_snippets: List[str] = []
  for q in queries[:5]:
    batch = await _serp_query(q, max_urls=8)
    for u in (batch.get("urls") or []):
      if u not in urls:
        urls.append(u)
    for s in (batch.get("snippets") or []):
      if s and s not in seed_snippets:
        seed_snippets.append(s)

  await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="serper", operation="query_dense_reviews")
  page_snippets = await _collect_snippets(urls, max_total_snippets=48, per_url_limit=6)
  snippets: List[str] = []
  for s in seed_snippets + page_snippets:
    if s and s not in snippets:
      snippets.append(s)
  return {
    "query": " | ".join(queries[:5]),
    "urls": urls[:8],
    "snippets": snippets[:64],
    "cached_at": _iso_now(),
    "cache_hit": False,
  }


async def _get_domain_website_copy(
  *,
  domain: str,
  db: AsyncSession,
  scan_kind: str,
  scan_id=None,
  preview_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
  preview_cached = _extract_website_cache(preview_data or {})
  if preview_cached:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="cache", operation="reuse_website_48h_preview_payload")
    return preview_cached

  result = await db.execute(select(PreviewScan).where(PreviewScan.domain == domain).limit(1))
  row = result.scalar_one_or_none()
  db_cached = _extract_website_cache((row.preview_json if row else {}) or {})
  if db_cached:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="cache", operation="reuse_website_48h_domain_cache")
    return db_cached

  start_urls = [f"https://{domain}", f"https://www.{domain}", f"http://{domain}"]
  homepage: Dict[str, Any] = {}
  for u in start_urls:
    candidate = await _fetch_html(u)
    if candidate.get("html"):
      homepage = candidate
      break
  await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="web", operation="fetch_homepage")

  if not homepage.get("html"):
    fallback_copy = {
      "headline": "",
      "subheadline": "",
      "hero_positioning_statement": "",
      "feature_sections": [],
      "pricing_page_content": "Pricing page not found.",
      "faq_copy": [],
      "raw_website_excerpt": "",
    }
    return {"website_copy": fallback_copy, "cached_at": _iso_now(), "cache_hit": False}

  links = _extract_anchor_hrefs(str(homepage.get("html") or ""), str(homepage.get("url") or start_urls[0]))
  pricing_url = _pick_link(links, ["pricing", "plans", "plan", "billing", "price"])
  faq_url = _pick_link(links, ["faq", "help", "support", "questions"])

  tasks = []
  task_keys: List[str] = []
  if pricing_url:
    task_keys.append("pricing")
    tasks.append(_fetch_html(pricing_url))
  if faq_url and faq_url != pricing_url:
    task_keys.append("faq")
    tasks.append(_fetch_html(faq_url))
  fetched = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

  pages: Dict[str, Dict[str, Any]] = {"homepage": homepage}
  for i, key in enumerate(task_keys):
    payload = fetched[i]
    if isinstance(payload, Exception) or not isinstance(payload, dict):
      continue
    pages[key] = payload

  if pricing_url:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="web", operation="fetch_pricing_page")
  if faq_url:
    await log_api_usage(db, scan_kind=scan_kind, scan_id=scan_id, domain=domain, provider="web", operation="fetch_faq_page")

  website_copy = _extract_website_copy_from_pages(pages)
  return {"website_copy": website_copy, "cached_at": _iso_now(), "cache_hit": False}


def _normalize_quotes(value: Any) -> List[str]:
  quotes = _list_of_strings(value, limit=2)
  cleaned: List[str] = []
  for quote in quotes:
    q = " ".join(str(quote).split())
    if q:
      cleaned.append(q[:180])
  return cleaned[:2]


def _normalize_cluster(value: Any) -> Optional[Dict[str, Any]]:
  if not isinstance(value, dict):
    return None
  name = str(value.get("cluster_name") or "").strip()
  description = str(value.get("description") or "").strip()
  user_goal = str(value.get("underlying_user_goal") or "").strip()
  why_fix = str(value.get("why_competitor_struggles_to_fix") or "").strip()
  if not (name and description and user_goal and why_fix):
    return None
  frequency = str(value.get("estimated_frequency") or "").strip().lower()
  if frequency not in {"low", "medium", "high"}:
    frequency = "low"
  quotes = _normalize_quotes(value.get("example_quotes"))
  if len(quotes) < 2:
    return None
  return {
    "cluster_name": name,
    "description": description,
    "estimated_frequency": frequency,
    "example_quotes": quotes,
    "underlying_user_goal": user_goal,
    "why_competitor_struggles_to_fix": why_fix,
  }


GENERIC_WEDGE_PHRASES = {
  "improve onboarding",
  "optimize ux",
  "improve ux",
  "enhance user experience",
  "streamline workflow",
  "make it easier",
  "better support",
  "increase engagement",
}


def _looks_generic(text: str) -> bool:
  low = " ".join(text.lower().split())
  if len(low) < 22:
    return True
  return any(phrase in low for phrase in GENERIC_WEDGE_PHRASES)


def _normalize_dominant_wedge(value: Any) -> Optional[Dict[str, Any]]:
  if not isinstance(value, dict):
    return None
  problem = str(value.get("problem_to_attack") or "").strip()
  target = str(value.get("who_to_target") or "").strip()
  build_first = str(value.get("what_to_build_first") or "").strip()
  why = str(value.get("why_this_is_high_leverage") or "").strip()
  ignore = _list_of_strings(value.get("what_to_ignore_initially"), limit=4)
  if not (problem and target and build_first and why and ignore):
    return None
  if _looks_generic(problem) or _looks_generic(build_first):
    return None
  return {
    "problem_to_attack": problem,
    "who_to_target": target,
    "what_to_build_first": build_first,
    "why_this_is_high_leverage": why,
    "what_to_ignore_initially": ignore,
  }


def _normalize_execution_plan_30d(value: Any) -> List[str]:
  raw = _list_of_strings(value, limit=8)
  out: List[str] = []
  for item in raw:
    step = item.strip()
    if step:
      out.append(step)
  return out[:4]


def _normalize_positioning_analysis(value: Any) -> Dict[str, Any]:
  data = value if isinstance(value, dict) else {}
  return {
    "target_audience_detected": str(data.get("target_audience_detected") or "").strip(),
    "value_proposition": str(data.get("value_proposition") or "").strip(),
    "positioning_specificity_score": max(1, min(10, int(float(data.get("positioning_specificity_score", 4) or 4)))),
    "proof_present": bool(data.get("proof_present")),
    "positioning_weaknesses": _list_of_strings(data.get("positioning_weaknesses"), limit=4),
    "positioning_opportunities": _list_of_strings(data.get("positioning_opportunities"), limit=4),
  }


def _normalize_feature_gap_analysis(value: Any) -> Dict[str, Any]:
  data = value if isinstance(value, dict) else {}
  return {
    "explicit_features": _list_of_strings(data.get("explicit_features"), limit=10),
    "inferred_missing_features": _list_of_strings(data.get("inferred_missing_features"), limit=8),
    "high_leverage_gap": str(data.get("high_leverage_gap") or "").strip(),
  }


def _normalize_pricing_analysis(value: Any) -> Dict[str, Any]:
  data = value if isinstance(value, dict) else {}
  return {
    "pricing_model": str(data.get("pricing_model") or "").strip(),
    "tier_structure_clarity": max(1, min(10, int(float(data.get("tier_structure_clarity", 4) or 4)))),
    "monetization_weaknesses": _list_of_strings(data.get("monetization_weaknesses"), limit=4),
    "upsell_opportunities": _list_of_strings(data.get("upsell_opportunities"), limit=4),
  }


def _fallback_wedge_from_inference(
  domain: str,
  positioning: Dict[str, Any],
  features: Dict[str, Any],
  pricing: Dict[str, Any],
  clusters: List[Dict[str, Any]],
) -> Dict[str, Any]:
  explicit_features = features.get("explicit_features") or []
  missing_features = features.get("inferred_missing_features") or []
  pricing_weaknesses = pricing.get("monetization_weaknesses") or []
  positioning_weaknesses = positioning.get("positioning_weaknesses") or []

  problem_parts = []
  if positioning_weaknesses:
    problem_parts.append(positioning_weaknesses[0])
  if features.get("high_leverage_gap"):
    problem_parts.append(features["high_leverage_gap"])
  elif missing_features:
    problem_parts.append(missing_features[0])
  if pricing_weaknesses:
    problem_parts.append(pricing_weaknesses[0])
  if clusters:
    problem_parts.append(clusters[0]["cluster_name"])
  problem = " + ".join(part for part in problem_parts if part)[:240] or f"Unclear buyer decision criteria in the {domain} category"
  target = positioning.get("target_audience_detected") or f"Buyers actively evaluating alternatives to {domain}"
  build_first = features.get("high_leverage_gap") or (missing_features[0] if missing_features else f"Single-purpose capability that resolves '{problem[:70]}'")
  why = (
    f"This is high leverage because it directly addresses '{problem[:100]}' for {target}, while "
    f"the current offer exposes gaps in positioning clarity, feature surface, or pricing structure."
  )
  ignore = explicit_features[-2:] if len(explicit_features) >= 2 else explicit_features[:]
  if len(ignore) < 2:
    ignore.extend((pricing.get("upsell_opportunities") or [])[:2 - len(ignore)])
  if len(ignore) < 2:
    ignore.extend((positioning.get("positioning_opportunities") or [])[:2 - len(ignore)])
  if len(ignore) < 2:
    ignore.append(f"Feature work unrelated to '{build_first[:80]}'")
  if len(ignore) < 2:
    ignore.append(f"Secondary segment requests outside '{target[:80]}'")
  return {
    "problem_to_attack": problem,
    "who_to_target": str(target)[:220],
    "what_to_build_first": str(build_first)[:220],
    "why_this_is_high_leverage": why[:360],
    "what_to_ignore_initially": ignore[:3],
  }


def _build_founder_dashboard(
  domain: str,
  wedge: Dict[str, Any],
  plan: List[str],
  positioning: Dict[str, Any],
  features: Dict[str, Any],
  pricing: Dict[str, Any],
  clusters: List[Dict[str, Any]],
  urls: List[str],
  strengths: List[str],
  weaknesses: List[str],
  complaints: List[str],
  what_best: str,
  evidence_snippets: List[str],
  user_requests: List[str],
  core_desire: str,
  quick_wins: List[str],
  personas: List[str],
  business_impact_lines: List[str],
  tactical_playbook: List[str],
  advantage_breakdown: List[Dict[str, Any]],
  compare_mode: str = "solo",
  user_product_description: Optional[str] = None,
  better_than_you_points: Optional[List[str]] = None,
) -> Dict[str, Any]:
  top_cluster = clusters[0] if clusters else None
  strengths = _clean_signal_list(strengths, limit=4)
  weaknesses = _clean_signal_list(weaknesses, limit=5)
  complaints = _clean_signal_list(complaints, limit=6)
  what_best_clean = "" if _is_placeholder_text(what_best) else str(what_best).strip()

  if not strengths:
    strengths = _clean_signal_list(
      [
        positioning.get("value_proposition"),
        positioning.get("target_audience_detected"),
      ]
      + _list_of_strings(features.get("explicit_features"), limit=4),
      limit=3,
    )
  if not weaknesses:
    weaknesses = _clean_signal_list(
      _list_of_strings(positioning.get("positioning_weaknesses"), limit=4)
      + _list_of_strings(pricing.get("monetization_weaknesses"), limit=4),
      limit=4,
    )
  if not complaints:
    complaints = _clean_signal_list(
      [clusters[0]["cluster_name"]] if clusters else [],
      limit=3,
    )
  if not complaints:
    complaints = _clean_signal_list(evidence_snippets, limit=3)

  opportunities = _clean_signal_list(
    [
      wedge.get("problem_to_attack"),
      features.get("high_leverage_gap"),
    ]
    + _list_of_strings(features.get("inferred_missing_features"), limit=4)
    + _list_of_strings(pricing.get("upsell_opportunities"), limit=3),
    limit=6,
  )
  if not opportunities:
    opportunities = [wedge.get("problem_to_attack") or f"Build around unresolved complaints in {domain}"]

  outperform_moves = _clean_signal_list(
    [
      wedge.get("what_to_build_first"),
      wedge.get("why_this_is_high_leverage"),
    ] + _list_of_strings(wedge.get("what_to_ignore_initially"), limit=3),
    limit=5,
  )

  user_requests = _clean_signal_list(user_requests, limit=6)
  if not user_requests:
    user_requests = _clean_signal_list(complaints, limit=4)
  core_desire = "" if _is_placeholder_text(core_desire) else str(core_desire).strip()
  if not core_desire:
    core_desire = f"Users want a smoother, more reliable experience than {domain} currently delivers."

  quick_wins = _clean_signal_list(quick_wins, limit=3)
  if len(quick_wins) < 3:
    quick_wins = (_clean_signal_list(quick_wins + [
      "Instrument automatic crash/error reporting on high-traffic flows.",
      "Reduce initial page/app payload and defer non-critical assets.",
      "Improve resume/retry handling for interrupted user sessions.",
    ], limit=3))

  personas = _clean_signal_list(personas, limit=5)
  if not personas:
    personas = _clean_signal_list(
      [positioning.get("target_audience_detected"), wedge.get("who_to_target")],
      limit=3,
    )

  business_impact_lines = _clean_signal_list(business_impact_lines, limit=5)
  if not business_impact_lines:
    business_impact_lines = [
      "Retention uplift potential: +4% to +10% if top complaints are resolved.",
      "Higher conversion from reduced friction and clearer value delivery.",
      "Lower support burden from fewer repeat reliability incidents.",
    ]

  tactical_playbook = _clean_signal_list(tactical_playbook, limit=6)
  if not tactical_playbook:
    tactical_playbook = _clean_signal_list(
      [
        wedge.get("what_to_build_first"),
        "Set measurable SLAs for reliability/performance and track weekly.",
        "Ship fixes behind feature flags and monitor impact by segment/device.",
        "Close the loop with affected users and verify pain-point reduction.",
      ],
      limit=5,
    )

  cleaned_advantages: List[Dict[str, str]] = []
  for row in advantage_breakdown or []:
    if not isinstance(row, dict):
      continue
    adv = str(row.get("advantage") or "").strip()
    impact = str(row.get("impact_size") or "").strip()
    why_row = str(row.get("why_it_matters") or "").strip()
    if _is_placeholder_text(adv):
      continue
    if not impact:
      impact = "Medium"
    cleaned_advantages.append({
      "advantage": adv,
      "impact_size": impact,
      "why_it_matters": why_row or "This creates a measurable edge versus the current competitor experience.",
    })
    if len(cleaned_advantages) >= 5:
      break
  if not cleaned_advantages:
    cleaned_advantages = [
      {
        "advantage": str(wedge.get("what_to_build_first") or "Focused solution to top complaint cluster"),
        "impact_size": "High",
        "why_it_matters": str(wedge.get("why_this_is_high_leverage") or "Directly addresses the highest-friction moment for users."),
      }
    ]

  sections = {
    "Key Strengths": "\n".join(f"- {x}" for x in strengths),
    "Key Weaknesses": "\n".join(f"- {x}" for x in weaknesses),
    "Common Complaints": "\n".join(f"- {x}" for x in complaints),
    "Opportunities for a New Competitor": "\n".join(f"- {x}" for x in opportunities),
    "How to Outperform This Competitor": "\n".join(f"- {x}" for x in outperform_moves),
    "What Users Are Actually Asking For": "\n".join(
      [f"- {x}" for x in user_requests] + [f"- Core desire: \"{core_desire}\""]
    ),
    "3 Fast Wins (Ship In 7 Days)": "\n".join(
      [f"- {idx + 1}) {x}" for idx, x in enumerate(quick_wins)]
    ),
    "Who Should Care About This": "\n".join(
      [f"- {x}" for x in personas]
    ),
    "Potential Business Impact": "\n".join(
      [f"- {x}" for x in business_impact_lines]
    ),
    "How To Execute This Opportunity": "\n".join(
      [f"- {idx + 1}) {x}" for idx, x in enumerate(tactical_playbook)]
    ),
    "Competitor Advantage Breakdown": "\n".join(
      [f"- {x['advantage']} (Impact: {x['impact_size']}) — {x['why_it_matters']}" for x in cleaned_advantages]
    ),
    "The #1 Strategic Move": "\n".join(
      [
        f"- Problem to attack: {wedge['problem_to_attack']}",
        f"- Who it targets: {wedge['who_to_target']}",
        f"- What to build first: {wedge['what_to_build_first']}",
        f"- Why this wins: {wedge['why_this_is_high_leverage']}",
      ]
    ),
    "Supporting Strategic Gaps": "\n".join(
      [f"- Positioning weakness: {x}" for x in positioning.get("positioning_weaknesses", [])[:3]]
      + [f"- Feature gap: {x}" for x in features.get("inferred_missing_features", [])[:3]]
      + [f"- Pricing weakness: {x}" for x in pricing.get("monetization_weaknesses", [])[:3]]
      + ([f"- Top complaint cluster: {top_cluster['cluster_name']}"] if top_cluster else [])
    ),
    "30-Day Execution Plan": "\n".join(f"- {x}" for x in plan),
  }
  better_points = _clean_signal_list(better_than_you_points or [], limit=10)
  if compare_mode == "own" and user_product_description and better_points:
    sections["What They Do Better Than You"] = "\n".join(f"- {x}" for x in better_points)

  report = (
    "## The #1 Strategic Move\n"
    f"- Problem to attack: {wedge['problem_to_attack']}\n"
    f"- Who it targets: {wedge['who_to_target']}\n"
    f"- What to build first: {wedge['what_to_build_first']}\n"
    f"- Why this wins: {wedge['why_this_is_high_leverage']}\n"
    f"- What to ignore initially: {', '.join(wedge['what_to_ignore_initially'])}\n\n"
    "## Supporting Strategic Gaps\n"
    + "\n".join([f"- {x}" for x in positioning.get("positioning_weaknesses", [])[:3]])
    + ("\n" if positioning.get("positioning_weaknesses") else "")
    + "\n".join([f"- {x}" for x in features.get("inferred_missing_features", [])[:3]])
    + ("\n" if features.get("inferred_missing_features") else "")
    + "\n".join([f"- {x}" for x in pricing.get("monetization_weaknesses", [])[:3]])
    + ("\n" if pricing.get("monetization_weaknesses") else "")
    + (f"- Top complaint cluster: {top_cluster['cluster_name']}" if top_cluster else "- Top complaint cluster: Low review density signal")
    + "\n\n## 30-Day Execution Plan\n"
    + "\n".join(f"- {item}" for item in plan)
  )

  opportunity_score = max(
    5,
    min(
      10,
      int(round((11 - positioning.get("positioning_specificity_score", 5)) * 0.4))
      + (2 if features.get("high_leverage_gap") else 0)
      + (1 if pricing.get("monetization_weaknesses") else 0),
    ),
  )

  return {
    "sections": sections,
    "report": report,
    "sentiment_badge": "Mixed",
    "opportunity_score": opportunity_score,
    "difficulty_score": 5,
    "biggest_opportunity": wedge["problem_to_attack"],
    "biggest_strength": what_best_clean or (strengths[0] if strengths else (positioning.get("value_proposition", "") or "Strong brand recognition and distribution.")),
    "biggest_weakness": (complaints[0] if complaints else (weaknesses[0] if weaknesses else wedge["problem_to_attack"])),
    "confidence": "High" if top_cluster else "Medium",
    "sources": urls[:8],
    "quotes": (
      [{"text": q, "source": "Review evidence"} for cluster in clusters[:3] for q in cluster["example_quotes"][:2]]
      or [{"text": s, "source": "Serper snippet"} for s in evidence_snippets[:3]]
    ),
    "opportunity_meta": [
      {"demand": "High", "impact": row.get("impact_size", "Medium")}
      for row in cleaned_advantages[:6]
    ],
    "what_users_want": {
      "user_requests": user_requests,
      "core_desire": core_desire,
    },
    "quick_wins_7d": quick_wins,
    "user_persona_fit": personas,
    "business_impact_projection": business_impact_lines,
    "tactical_playbook": tactical_playbook,
    "competitor_advantage_breakdown": cleaned_advantages,
    "compare_mode": compare_mode,
    "user_product_description": user_product_description if compare_mode == "own" and user_product_description else None,
    "competitor_better_than_you": better_points if compare_mode == "own" and user_product_description else [],
    "dominant_wedge": wedge,
    "execution_plan_30_days": plan,
    "positioning_analysis": positioning,
    "feature_gap_analysis": features,
    "pricing_analysis": pricing,
    "complaint_clusters": clusters[:3],
    "top_complaint_cluster": top_cluster,
    "low_signal": False,
    "message": "",
  }


async def run_preview_scan(domain: str, db: AsyncSession, scan_id=None) -> Dict[str, Any]:
  bundle = await _get_domain_snippets(domain=domain, db=db, scan_kind="preview", scan_id=scan_id)
  website_bundle = await _get_domain_website_copy(domain=domain, db=db, scan_kind="preview", scan_id=scan_id)
  snippets = _normalize_snippets(bundle.get("snippets"), max_items=64)
  urls = _normalize_urls(bundle.get("urls"), max_items=8)
  website_copy = website_bundle.get("website_copy") if isinstance(website_bundle.get("website_copy"), dict) else {}

  # Optional Play Store enrichment, only for likely mobile-app targets.
  play_package = await _discover_play_store_package(domain, website_copy)
  if play_package:
    await log_api_usage(
      db,
      scan_kind="full",
      scan_id=scan_id,
      domain=domain,
      provider="serper",
      operation="discover_play_store_package",
    )
    play_data = await _collect_play_store_snippets(play_package)
    play_snippets = _normalize_snippets(play_data.get("snippets"), max_items=48)
    if play_snippets:
      await log_api_usage(
        db,
        scan_kind="full",
        scan_id=scan_id,
        domain=domain,
        provider="play_store",
        operation="fetch_reviews",
      )
      snippets = _normalize_snippets(snippets + play_snippets, max_items=96)
      play_url = str(play_data.get("source_url") or "").strip()
      if play_url and play_url not in urls:
        urls = _normalize_urls([play_url] + urls, max_items=8)
  serp_cache = _build_serp_cache(str(bundle.get("query") or ""), urls, snippets)
  website_cache = _build_website_cache(website_copy)

  preview_prompt = (
    f"Domain: {domain}\n\n"
    "You are a product strategist creating a teaser from mixed public signals.\n"
    "Use website copy and review snippets (if present). Return strict JSON with keys:\n"
    "{"
    "\"sentiment_score\": number (0-100),"
    "\"negative_percent_estimate\": number,"
    "\"positive_percent_estimate\": number,"
    "\"top_pain_points\": array (max 3),"
    "\"top_strengths\": array (max 2),"
    "\"opportunity_score\": number (0-100)"
    "}\n\n"
    "Website copy:\n"
    f"{json.dumps(website_copy)}\n\n"
    "Review snippets:\n"
    + ("\n".join(f"- {s}" for s in snippets[:20]) if snippets else "- No high-density complaint snippets found.")
  )
  data = await _openai_json(preview_prompt, max_tokens=500)
  await log_api_usage(db, scan_kind="preview", scan_id=scan_id, domain=domain, provider="openai", operation="preview_structured_json")

  pain_points = _list_of_strings(data.get("top_pain_points"), limit=3)
  strengths = _list_of_strings(data.get("top_strengths"), limit=2)
  if not pain_points:
    pain_points = _list_of_strings(website_copy.get("feature_sections"), limit=3)
  if not strengths:
    strengths = _list_of_strings([website_copy.get("value_proposition") or website_copy.get("headline")], limit=2)

  return {
    "sentiment_score": _int_0_100(data.get("sentiment_score"), 50),
    "negative_percent_estimate": _int_0_100(data.get("negative_percent_estimate"), 50),
    "positive_percent_estimate": _int_0_100(data.get("positive_percent_estimate"), 50),
    "top_pain_points": pain_points,
    "top_strengths": strengths,
    "opportunity_score": _int_0_100(data.get("opportunity_score"), 60),
    "recurring_feature_requests_hidden": max(0, len(pain_points)),
    "unlock_reviews_count": max(len(snippets), len(_list_of_strings(website_copy.get("feature_sections"), limit=20))),
    "source_urls": urls,
    "serp_cache": serp_cache,
    "website_cache": website_cache,
    "low_signal": False if snippets or website_copy.get("raw_website_excerpt") else True,
    "message": "" if (snippets or website_copy.get("raw_website_excerpt")) else LOW_SIGNAL_MESSAGE,
  }


async def run_full_scan(
  domain: str,
  preview_data: Dict[str, Any],
  db: AsyncSession,
  scan_id=None,
  base_url: Optional[str] = None,
  compare_mode: str = "solo",
  user_product_description: Optional[str] = None,
) -> Dict[str, Any]:
  _ = base_url
  compare_mode_normalized = (compare_mode or "solo").strip().lower()
  product_description = (user_product_description or "").strip()
  if compare_mode_normalized != "own" or not product_description:
    compare_mode_normalized = "solo"
    product_description = ""
  bundle = await _get_domain_snippets(
    domain=domain,
    db=db,
    scan_kind="full",
    scan_id=scan_id,
    preview_data=preview_data,
  )
  website_bundle = await _get_domain_website_copy(
    domain=domain,
    db=db,
    scan_kind="full",
    scan_id=scan_id,
    preview_data=preview_data,
  )
  snippets = _normalize_snippets(bundle.get("snippets"), max_items=64)
  urls = _normalize_urls(bundle.get("urls"), max_items=8)
  website_copy = website_bundle.get("website_copy") if isinstance(website_bundle.get("website_copy"), dict) else {}

  positioning_prompt = (
    "You are a product strategist.\n\n"
    "From the website copy below:\n\n"
    "Identify:\n"
    "- Stated target audience\n"
    "- Core value proposition\n"
    "- Claimed differentiator\n"
    "- Primary promise\n\n"
    "Evaluate:\n"
    "- Is the positioning specific or generic?\n"
    "- Is there quantified proof?\n"
    "- Is there urgency?\n"
    "- Is the ICP clearly defined?\n\n"
    "Return structured JSON:\n"
    "{\n"
    "\"target_audience_detected\": \"...\",\n"
    "\"value_proposition\": \"...\",\n"
    "\"positioning_specificity_score\": 1,\n"
    "\"proof_present\": false,\n"
    "\"positioning_weaknesses\": [\"...\", \"...\"],\n"
    "\"positioning_opportunities\": [\"...\", \"...\"]\n"
    "}\n\n"
    f"Website copy:\n{json.dumps(website_copy)}"
  )
  positioning_raw = await _openai_json(positioning_prompt, max_tokens=800, system_prompt="You are a product strategist.\nReturn strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="positioning_analysis")
  positioning = _normalize_positioning_analysis(positioning_raw)

  feature_prompt = (
    "From the website copy below, list explicitly mentioned features.\n"
    "Then infer likely missing features competitors in this category often include.\n\n"
    "Return JSON:\n"
    "{\n"
    "\"explicit_features\": [...],\n"
    "\"inferred_missing_features\": [...],\n"
    "\"high_leverage_gap\": \"...\"\n"
    "}\n\n"
    f"Website copy:\n{json.dumps(website_copy)}"
  )
  feature_raw = await _openai_json(feature_prompt, max_tokens=800, system_prompt="Return strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="feature_gap_inference")
  features = _normalize_feature_gap_analysis(feature_raw)

  pricing_prompt = (
    "Analyze pricing structure from the copy below.\n"
    "If pricing page exists, extract pricing model, tier clarity, monetization weaknesses, and upsell opportunities.\n"
    "If no pricing page exists, infer monetization weakness from absence.\n\n"
    "Return JSON:\n"
    "{\n"
    "\"pricing_model\": \"...\",\n"
    "\"tier_structure_clarity\": 1,\n"
    "\"monetization_weaknesses\": [...],\n"
    "\"upsell_opportunities\": [...]\n"
    "}\n\n"
    f"Website pricing content:\n{website_copy.get('pricing_page_content') or 'Pricing page not found.'}"
  )
  pricing_raw = await _openai_json(pricing_prompt, max_tokens=700, system_prompt="Return strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="pricing_structure_analysis")
  pricing = _normalize_pricing_analysis(pricing_raw)

  clusters: List[Dict[str, Any]] = []
  if len(snippets) >= MIN_SNIPPETS_FOR_COMPLAINTS:
    complaints_prompt = (
      f"Domain: {domain}\n\n"
      "Extract recurring complaint clusters from snippets.\n"
      "Return JSON:\n"
      "{\n"
      "\"complaint_clusters\": [\n"
      "{\n"
      "\"cluster_name\": \"...\",\n"
      "\"description\": \"...\",\n"
      "\"estimated_frequency\": \"low|medium|high\",\n"
      "\"example_quotes\": [\"...\", \"...\"],\n"
      "\"underlying_user_goal\": \"...\",\n"
      "\"why_competitor_struggles_to_fix\": \"...\"\n"
      "}\n"
      "]\n"
      "}\n\n"
      "Evidence snippets:\n"
      + "\n".join(f"- {snippet}" for snippet in snippets[:40])
    )
    complaints_raw = await _openai_json(complaints_prompt, max_tokens=1100, system_prompt="Return strict JSON only.")
    await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="complaint_clustering_optional")
    clusters = [c for c in (_normalize_cluster(item) for item in (complaints_raw.get("complaint_clusters") or [])) if c][:3]

  synth_prompt = (
    "Based on:\n"
    "- Positioning weaknesses\n"
    "- Feature gaps\n"
    "- Pricing weaknesses\n"
    "- Complaint clusters (if any)\n\n"
    "Define ONE dominant product wedge.\n\n"
    "Return JSON:\n"
    "{\n"
    "\"dominant_wedge\": {\n"
    "\"problem_to_attack\": \"...\",\n"
    "\"who_to_target\": \"...\",\n"
    "\"what_to_build_first\": \"...\",\n"
    "\"why_this_is_high_leverage\": \"...\",\n"
    "\"what_to_ignore_initially\": [\"...\", \"...\"]\n"
    "},\n"
    "\"execution_plan_30_days\": [\n"
    "\"Week 1: ...\",\n"
    "\"Week 2: ...\",\n"
    "\"Week 3: ...\",\n"
    "\"Week 4: ...\"\n"
    "]\n"
    "}\n\n"
    "The wedge must be specific, buildable, and non-generic.\n"
    "Do not output generic moves such as 'improve onboarding' or 'optimize UX'.\n\n"
    f"Positioning analysis:\n{json.dumps(positioning)}\n\n"
    f"Feature gap analysis:\n{json.dumps(features)}\n\n"
    f"Pricing analysis:\n{json.dumps(pricing)}\n\n"
    f"Complaint clusters:\n{json.dumps(clusters)}"
  )
  synth_raw = await _openai_json(synth_prompt, max_tokens=1100, system_prompt="You are a product strategist.\nReturn strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="dominant_wedge_synthesis")

  wedge = _normalize_dominant_wedge(synth_raw.get("dominant_wedge"))
  plan = _normalize_execution_plan_30d(synth_raw.get("execution_plan_30_days"))
  if not wedge or len(plan) < 4:
    regen_prompt = synth_prompt + "\n\nRegenerate. Be concrete and avoid generic advice."
    regen_raw = await _openai_json(regen_prompt, max_tokens=1200, system_prompt="You are a product strategist.\nReturn strict JSON only.")
    await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="dominant_wedge_regeneration")
    wedge = _normalize_dominant_wedge(regen_raw.get("dominant_wedge"))
    plan = _normalize_execution_plan_30d(regen_raw.get("execution_plan_30_days"))

  if not wedge:
    wedge = _fallback_wedge_from_inference(domain, positioning, features, pricing, clusters)
  if len(plan) < 4:
    plan = [
      f"Week 1: Interview target users from {wedge['who_to_target']} and validate '{wedge['problem_to_attack']}' with 10 calls.",
      f"Week 2: Ship a scoped prototype for '{wedge['what_to_build_first']}' and test with 3 design partners.",
      "Week 3: Measure activation + conversion against incumbent alternatives and tighten the implementation.",
      "Week 4: Launch a paid pilot with explicit success criteria and prune non-core scope from the build queue.",
    ]

  market_signal_prompt = (
    f"Domain: {domain}\n\n"
    "You are producing customer-intelligence signals from review snippets and available website copy.\n"
    "Return strict JSON with keys:\n"
    "{\n"
    "\"what_competitor_does_best\": \"single sentence\",\n"
    "\"top_strengths\": [\"...\"],\n"
    "\"top_weaknesses\": [\"...\"],\n"
    "\"top_customer_complaints\": [\"...\"]\n"
    "}\n\n"
    "Rules:\n"
    "- Never output placeholders like 'unable to determine', 'unknown', or 'access denied'.\n"
    "- If website copy is weak, infer from review snippets only.\n"
    "- Keep each bullet concrete and specific.\n\n"
    f"Website copy:\n{json.dumps(website_copy)}\n\n"
    "Review snippets:\n"
    + ("\n".join(f"- {snippet}" for snippet in snippets[:50]) if snippets else "- No snippets collected.")
  )
  market_raw = await _openai_json(market_signal_prompt, max_tokens=900, system_prompt="Return strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="strengths_weaknesses_complaints")
  strengths = _clean_signal_list(market_raw.get("top_strengths"), limit=4)
  weaknesses = _clean_signal_list(market_raw.get("top_weaknesses"), limit=5)
  complaints = _clean_signal_list(market_raw.get("top_customer_complaints"), limit=6)
  what_best = str(market_raw.get("what_competitor_does_best") or "").strip()

  consultant_prompt = (
    f"Domain: {domain}\n\n"
    "Create practical consulting outputs from review evidence. Return strict JSON:\n"
    "{\n"
    "\"what_users_actually_want\": [\"...\"],\n"
    "\"core_desire\": \"...\",\n"
    "\"quick_wins_7d\": [\"...\", \"...\", \"...\"],\n"
    "\"who_should_care\": [\"...\"],\n"
    "\"business_impact_projection\": [\"...\"],\n"
    "\"tactical_playbook\": [\"...\"],\n"
    "\"competitor_advantage_breakdown\": [\n"
    "  {\"advantage\": \"...\", \"impact_size\": \"High|Medium|Low\", \"why_it_matters\": \"...\"}\n"
    "]\n"
    "}\n\n"
    "Rules:\n"
    "- Make each item specific and actionable.\n"
    "- No placeholders or generic filler.\n"
    "- Keep impact claims realistic and evidence-linked.\n\n"
    f"Top complaint clusters:\n{json.dumps(clusters)}\n\n"
    f"Key snippets:\n{json.dumps(snippets[:40])}\n\n"
    f"Wedge:\n{json.dumps(wedge)}"
  )
  consultant_raw = await _openai_json(consultant_prompt, max_tokens=1200, system_prompt="You are a senior product strategy consultant. Return strict JSON only.")
  await log_api_usage(db, scan_kind="full", scan_id=scan_id, domain=domain, provider="openai", operation="consultant_playbook_generation")
  user_requests = _clean_signal_list(consultant_raw.get("what_users_actually_want"), limit=6)
  core_desire = str(consultant_raw.get("core_desire") or "").strip()
  quick_wins = _clean_signal_list(consultant_raw.get("quick_wins_7d"), limit=3)
  personas = _clean_signal_list(consultant_raw.get("who_should_care"), limit=5)
  business_impact_lines = _clean_signal_list(consultant_raw.get("business_impact_projection"), limit=5)
  tactical_playbook = _clean_signal_list(consultant_raw.get("tactical_playbook"), limit=6)
  advantage_breakdown = consultant_raw.get("competitor_advantage_breakdown") if isinstance(consultant_raw.get("competitor_advantage_breakdown"), list) else []

  better_than_you_points: List[str] = []
  if compare_mode_normalized == "own" and product_description:
    better_prompt = (
      f"Competitor domain: {domain}\n\n"
      "User's product description:\n"
      f"{product_description}\n\n"
      "You are a brutally honest product strategy advisor.\n"
      "Return strict JSON:\n"
      "{\n"
      "\"what_they_do_better_than_you\": [\"...\"]\n"
      "}\n\n"
      "Requirements:\n"
      "- Return 5 to 10 bullet points.\n"
      "- Each point must be specific and actionable for product improvement.\n"
      "- Focus on where the competitor currently outperforms the user's product.\n"
      "- Avoid generic statements and avoid placeholder text.\n\n"
      f"Competitor strengths:\n{json.dumps(strengths)}\n\n"
      f"Competitor weaknesses:\n{json.dumps(weaknesses)}\n\n"
      f"Competitor complaints:\n{json.dumps(complaints)}\n\n"
      f"Dominant wedge and plan:\n{json.dumps({'dominant_wedge': wedge, 'execution_plan_30_days': plan})}\n\n"
      f"Evidence snippets:\n{json.dumps(snippets[:35])}\n"
    )
    better_raw = await _openai_json(
      better_prompt,
      max_tokens=1000,
      system_prompt="You are a senior product strategy consultant. Return strict JSON only."
    )
    await log_api_usage(
      db,
      scan_kind="full",
      scan_id=scan_id,
      domain=domain,
      provider="openai",
      operation="compare_better_than_you",
    )
    better_than_you_points = _normalize_better_than_you_points(
      better_raw.get("what_they_do_better_than_you"),
      min_items=5,
      max_items=10,
    )

  return _build_founder_dashboard(
    domain=domain,
    wedge=wedge,
    plan=plan,
    positioning=positioning,
    features=features,
    pricing=pricing,
    clusters=clusters,
    urls=urls,
    strengths=strengths,
    weaknesses=weaknesses,
    complaints=complaints,
    what_best=what_best,
    evidence_snippets=snippets,
    user_requests=user_requests,
    core_desire=core_desire,
    quick_wins=quick_wins,
    personas=personas,
    business_impact_lines=business_impact_lines,
    tactical_playbook=tactical_playbook,
    advantage_breakdown=advantage_breakdown,
    compare_mode=compare_mode_normalized,
    user_product_description=product_description or None,
    better_than_you_points=better_than_you_points,
  )
