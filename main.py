import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import trafilatura
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from app.api.routes.auth import router as auth_router
from app.core.config import settings

from playstore import (
  PLAYSTORE_CACHE_HOURS,
  PLAYSTORE_ENABLED,
  PLAYSTORE_LOW_RATING_RATIO,
  PLAYSTORE_TARGET_REVIEWS,
  PlayStoreReview,
  fetch_play_store_reviews,
)


# Load .env in local/dev environments. On Railway, env vars are injected directly.
load_dotenv()

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

if not ANTHROPIC_API_KEY:
  raise RuntimeError("ANTHROPIC_API_KEY environment variable is required.")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

logger = logging.getLogger(__name__)

app = FastAPI(title="Customer Insight Explorer", version="0.4.0")
templates = Jinja2Templates(directory="templates")

# CORS
origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
app.add_middleware(
  CORSMiddleware,
  allow_origins=origins if origins != ["*"] else ["*"],
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

# Routers
app.include_router(auth_router)


class ProductContext(BaseModel):
  """Optional context about the user's own product to tailor strategy."""

  one_liner: str
  landing_url: Optional[str] = None
  homepage_copy: Optional[str] = None
  category: Optional[str] = None
  audience: Optional[str] = None


class AnalyzeRequest(BaseModel):
  query: str
  competitor: Optional[str] = None
  product: Optional[ProductContext] = None


def parse_report_sections(report: str) -> Dict[str, str]:
  """Parse markdown report into section title -> content. Uses ## headings."""
  sections: Dict[str, str] = {}
  if not report or not report.strip():
    return sections
  # Split by ## but keep the delimiter so we can capture section titles
  parts = re.split(r"\n(?=##\s+)", report.strip())
  for part in parts:
    part = part.strip()
    if not part:
      continue
    if part.startswith("## "):
      first_line, _, body = part.partition("\n")
      title = first_line.replace("##", "").strip()
      body = body.strip()
      if title:
        sections[title] = body
  return normalize_section_keys(sections)


# Canonical section keys the dashboard expects. Map common LLM variations to these.
SECTION_KEY_ALIASES: Dict[str, str] = {
  "overall sentiment": "Overall Sentiment",
  "key strengths": "Key Strengths",
  "what customers like": "Key Strengths",
  "strengths": "Key Strengths",
  "key weaknesses": "Key Weaknesses",
  "what customers dislike": "Key Weaknesses",
  "weaknesses": "Key Weaknesses",
  "common complaints": "Common Complaints",
  "complaints": "Common Complaints",
  "feature requests / unmet needs": "Feature Requests / Unmet Needs",
  "feature requests and unmet needs": "Feature Requests / Unmet Needs",
  "feature requests": "Feature Requests / Unmet Needs",
  "unmet needs": "Feature Requests / Unmet Needs",
  "opportunities for a new competitor": "Opportunities for a New Competitor",
  "opportunities for new competitors": "Opportunities for a New Competitor",
  "opportunities for a new or better competitor": "Opportunities for a New Competitor",
  "opportunities": "Opportunities for a New Competitor",
  "competitive comparison": "Competitive Comparison",
  "how to outperform this competitor": "How to Outperform This Competitor",
  "how to outperform": "How to Outperform This Competitor",
  "sources analyzed": "Sources Analyzed",
  "sources": "Sources Analyzed",
}


def normalize_section_keys(sections: Dict[str, str]) -> Dict[str, str]:
  """Map varied section titles to canonical keys so the dashboard always finds content."""
  out: Dict[str, str] = {}
  for raw_title, body in sections.items():
    key = raw_title.strip()
    if not key or not body:
      continue
    normalized = SECTION_KEY_ALIASES.get(key.lower())
    if normalized:
      # Append if we already have content for this key (e.g. from another alias)
      if normalized in out:
        out[normalized] = out[normalized] + "\n\n" + body
      else:
        out[normalized] = body
    else:
      # Keep unknown sections under original title so we don't lose data
      out[key] = body
  return out


def extract_quotes_from_report(report: str, max_length: int = 200) -> List[Dict[str, str]]:
  """Extract attributed quotes from report text (e.g. '...' — domain.com)."""
  quotes: List[Dict[str, str]] = []
  # Match quoted text followed by em dash and domain
  for m in re.finditer(r'"([^"]{20,})"\s*[—–-]\s*([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})', report):
    text, domain = m.group(1).strip(), m.group(2).strip()
    if len(text) > max_length:
      text = text[: max_length - 1].rstrip() + "…"
    quotes.append({"text": text, "domain": domain})
  # Also match lines that look like blockquote or bullet with quote
  for line in report.split("\n"):
    line = line.strip()
    if line.startswith("- ") or line.startswith("* "):
      inner = line[2:].strip()
      em = re.search(r'"([^"]{20,})"\s*[—–-]\s*([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})', inner)
      if em and not any(q["text"] == em.group(1) for q in quotes):
        text, domain = em.group(1).strip(), em.group(2).strip()
        if len(text) > max_length:
          text = text[: max_length - 1].rstrip() + "…"
        quotes.append({"text": text, "domain": domain})
  return quotes[:12]


def infer_sentiment_and_confidence(
  sections: Dict[str, str],
  sources_count: int,
) -> Tuple[str, str]:
  """Infer sentiment (Positive/Mixed/Negative) and confidence (High/Medium/Low)."""
  sentiment = "Mixed"
  overall = (sections.get("Overall Sentiment") or "").lower()
  strengths = (sections.get("Key Strengths") or "").lower()
  weaknesses = (sections.get("Key Weaknesses") or sections.get("Common Complaints") or "").lower()
  if "positive" in overall or "favorable" in overall or ("strong" in overall and "weakness" not in overall):
    sentiment = "Positive"
  elif "negative" in overall or "unfavorable" in overall or "frustrat" in overall:
    sentiment = "Negative"
  elif "mixed" in overall or "varied" in overall:
    sentiment = "Mixed"
  if not overall and strengths and not weaknesses:
    sentiment = "Positive"
  if not overall and weaknesses and not strengths:
    sentiment = "Negative"

  confidence = "Medium"
  if sources_count >= 5 and len(overall) > 50:
    confidence = "High"
  elif sources_count <= 2 or len(overall) < 20:
    confidence = "Low"
  return sentiment, confidence


def compute_scores(
  sections: Dict[str, str],
  report: str,
  sources: List["SourceInfo"],
) -> Dict[str, Any]:
  """
  Heuristic Opportunity Score (0–10) and Competitive Difficulty (0–10)
  based on complaints, severity, switching language, and lock-in markers.
  """
  weaknesses_text = " ".join(
    [
      sections.get("Key Weaknesses", "") or "",
      sections.get("Common Complaints", "") or "",
    ]
  ).lower()
  strengths_text = (sections.get("Key Strengths", "") or "").lower()
  full_text = (report or "").lower()

  severity_keywords = ["data loss", "lost data", "outage", "downtime", "offline", "crash", "corrupt", "billing", "overcharge", "security", "breach"]
  friction_keywords = ["slow", "lag", "latency", "performance", "bug", "glitch", "confusing", "complex", "hard to use", "clunky"]
  switching_keywords = ["switched to", "migrate", "migrated", "churn", "cancelled", "canceled", "left for", "looking for an alternative", "moved to"]
  lockin_keywords = ["ecosystem", "templates", "template library", "marketplace", "integrations", "plugin", "plugins", "add-ons", "community", "workflow", "automation"]

  def count_matches(text: str, keywords: List[str]) -> int:
    return sum(text.count(k) for k in keywords)

  severity_hits = count_matches(weaknesses_text, severity_keywords)
  friction_hits = count_matches(weaknesses_text, friction_keywords)
  switching_hits = count_matches(full_text, switching_keywords)
  lockin_hits = count_matches(strengths_text, lockin_keywords)

  complaints_len = len(weaknesses_text.split())
  complaint_density = complaints_len / max(len(full_text.split()), 1)

  # Map raw signals to coarse levels
  def level_from_hits(h: int) -> str:
    if h >= 6:
      return "High"
    if h >= 3:
      return "Medium"
    if h > 0:
      return "Low"
    return "None"

  severity_level = level_from_hits(severity_hits + friction_hits)
  switching_level = level_from_hits(switching_hits)
  if lockin_hits >= 6:
    lockin_level = "High"
  elif lockin_hits >= 3:
    lockin_level = "Medium"
  elif lockin_hits > 0:
    lockin_level = "Low"
  else:
    lockin_level = "None"

  if complaint_density > 0.06:
    complaint_freq = "High"
  elif complaint_density > 0.02:
    complaint_freq = "Medium"
  else:
    complaint_freq = "Low"

  src_count = len(sources)

  def score_for_level(level: str) -> int:
    return {"None": 0, "Low": 2, "Medium": 5, "High": 8}.get(level, 0)

  severity_score = score_for_level(severity_level)
  freq_score = score_for_level(complaint_freq)
  switching_score = score_for_level(switching_level)
  lockin_score = score_for_level(lockin_level)

  # Opportunity: more pain + switching, less lock-in
  raw_opp = severity_score * 0.4 + freq_score * 0.3 + switching_score * 0.3 - lockin_score * 0.3
  # Normalize and clamp
  opportunity_score = max(0, min(10, int(round(raw_opp / 0.8)) if raw_opp > 0 else int(round(raw_opp / 1.2))))

  # Difficulty: strong lock-in + strong positive sentiment + many sources
  positive_strength_markers = ["love", "rely", "can't live without", "standard", "default", "everyone uses"]
  pos_hits = count_matches(strengths_text, positive_strength_markers)
  pos_level = level_from_hits(pos_hits)
  pos_score = score_for_level(pos_level)

  if src_count >= 10:
    src_level = "High"
  elif src_count >= 5:
    src_level = "Medium"
  elif src_count > 0:
    src_level = "Low"
  else:
    src_level = "None"
  src_score = score_for_level(src_level)

  raw_diff = lockin_score * 0.45 + pos_score * 0.35 + src_score * 0.2
  difficulty_score = max(0, min(10, int(round(raw_diff / 0.9)) if raw_diff > 0 else 0))

  breakdown = {
    "severity": severity_level,
    "complaintFrequency": complaint_freq,
    "switchingLikelihood": switching_level,
    "lockIn": lockin_level,
    "sourceCountLevel": src_level,
  }

  return {
    "opportunityScore": opportunity_score,
    "difficultyScore": difficulty_score,
    "breakdown": breakdown,
  }


def structured_report_payload(
  report: str,
  sources: List["SourceInfo"],
  analyzed_at: datetime,
) -> Dict[str, Any]:
  """Build structured payload for dashboard: sections, quotes, scores, sentiment, confidence."""
  sections = parse_report_sections(report)
  quotes = extract_quotes_from_report(report)
  sentiment, confidence = infer_sentiment_and_confidence(sections, len(sources))
  scores = compute_scores(sections, report, sources)
  analyzed_at_str = analyzed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

  # First line of Overall Sentiment as headline; first bullet of Opportunities as biggest opportunity
  overall_text = sections.get("Overall Sentiment", "")
  first_bullet = re.search(r"^[-*]\s*(.+?)(?=\n[-*]|\n\n|$)", overall_text, re.S)
  sentiment_headline = (first_bullet.group(1).strip() if first_bullet else overall_text.split("\n")[0].strip())[:120] if overall_text else ""

  opps_text = sections.get("Opportunities for a New Competitor", "")
  opp_first = re.search(r"^[-*]\s*(.+?)(?=\n[-*]|\n\n|$)", opps_text, re.S)
  biggest_opportunity = (opp_first.group(1).strip() if opp_first else "")[:100] if opps_text else ""

  strengths_text = sections.get("Key Strengths", "")
  str_first = re.search(r"^[-*]\s*(.+?)(?=\n[-*]|\n\n|$)", strengths_text, re.S)
  biggest_strength = (str_first.group(1).strip() if str_first else "")[:100] if strengths_text else ""

  return {
    "report": report,
    "sections": sections,
    "quotes": quotes,
    "sentiment_badge": sentiment,
    "confidence": confidence,
    "analyzed_at": analyzed_at_str,
    "sources_count": len(sources),
    "opportunity_score": scores.get("opportunityScore"),
    "difficulty_score": scores.get("difficultyScore"),
    "score_breakdown": scores.get("breakdown"),
    "sentiment_headline": sentiment_headline or None,
    "biggest_opportunity": biggest_opportunity or None,
    "biggest_strength": biggest_strength or None,
  }


@dataclass
class SourceInfo:
  url: str
  domain: str
  kind: str
  quotes: List[str]


@dataclass
class AnalysisResult:
  report: str
  sources: List[SourceInfo]
  analyzed_at: datetime


# Simple in-memory cache for identical queries during a single process lifetime.
AnalysisCacheKey = Tuple[str, Optional[str]]
_analysis_cache: Dict[AnalysisCacheKey, AnalysisResult] = {}

# In-memory store for shareable reports (best-effort, non-persistent).
_shared_reports: Dict[str, Dict[str, Any]] = {}


PREFERRED_KEYWORDS = [
  "review",
  " vs ",
  "comparison",
  "reddit",
  "forum",
  "opinions",
  "alternatives",
  "g2",
  "capterra",
  "trustpilot",
  "blog",
]

PLAYSTORE_DISCOVERY_QUERIES = [
  'site:play.google.com/store/apps/details',
]

DISCARD_SUBSTRINGS = [
  "login",
  "sign-in",
  "signin",
  "sign_in",
  "signup",
  "sign-up",
  "store",
  "/cart",
  "/category",
  "/categories",
  "/pricing",
  "utm_",
  ".pdf",
]


def normalize_company_name(value: str) -> str:
  return value.strip().lower()


def build_brand_tokens(primary: str) -> List[str]:
  """
  Build a small set of lowercase tokens we expect to see in text
  when a page is truly about the given brand or domain.
  """
  normalized = primary.strip().lower()
  tokens = set()
  if not normalized:
    return []

  tokens.add(normalized)

  # If it's a domain, also include the hostname and first label.
  parsed = urlparse(normalized if "://" in normalized else f"http://{normalized}")
  host = (parsed.netloc or parsed.path or "").lower()
  if host:
    tokens.add(host)
    if "." in host:
      first_label = host.split(".")[0]
      if first_label:
        tokens.add(first_label)

  # Also break on dots, dashes, and spaces to get simpler parts.
  for part in normalized.replace(".", " ").replace("-", " ").split():
    part = part.strip()
    if part:
      tokens.add(part)

  return list(tokens)


def infer_source_kind(url: str) -> str:
  lower = url.lower()
  if "reddit.com" in lower or "forums." in lower or "/forum" in lower:
    return "discussion thread"
  if "g2.com" in lower or "capterra.com" in lower or "trustpilot.com" in lower:
    return "review page"
  if "blog." in lower or "/blog" in lower:
    return "blog"
  if "youtube.com" in lower or "youtu.be" in lower:
    return "video/comments"
  return "article"


def score_url_for_relevance(url: str) -> int:
  lower = url.lower()
  score = 0
  for kw in PREFERRED_KEYWORDS:
    if kw in lower:
      # Slightly higher weight for comparison-style and review pages.
      if kw in ("review", " vs ", "comparison", "g2", "capterra", "trustpilot"):
        score += 3
      else:
        score += 1
  return score


def is_discarded_url(url: str) -> bool:
  lower = url.lower()
  if any(bad in lower for bad in DISCARD_SUBSTRINGS):
    return True
  parsed = urlparse(url)
  # Skip obvious bare homepages unless they belong to review platforms.
  is_homepage = (parsed.path in ("", "/"))
  review_host = any(
    host in (parsed.netloc or "").lower()
    for host in ("g2.com", "capterra.com", "trustpilot.com")
  )
  if is_homepage and not review_host:
    return True
  return False


async def fetch_serp_urls(
  company: str,
  api_key: str,
  competitor: Optional[str] = None,
  max_urls: int = 6,
) -> List[str]:
  """Use SerpAPI to collect a small, high-signal set of organic URLs."""
  base = company.strip()
  search_queries: List[str] = [
    f"\"{base}\" reviews",
    f"\"{base}\" complaints",
    f"\"{base}\" problems",
    f"{base} site:trustpilot.com",
    f"{base} site:g2.com",
    f"{base} site:capterra.com",
    f"{base} alternatives",
    f"{base} vs competitors",
  ]
  if competitor:
    search_queries.extend(
      [
        f"{base} vs {competitor}",
        f"{competitor} vs {base}",
        f"{base} vs {competitor} reviews",
        f"{base} vs {competitor} reddit",
      ]
    )

  candidates: List[str] = []
  seen_urls = set()

  async with httpx.AsyncClient(timeout=10.0) as client:
    for q in search_queries:
      params = {
        "engine": "google",
        "q": q,
        "api_key": api_key,
      }
      try:
        resp = await client.get("https://serpapi.com/search.json", params=params)
        resp.raise_for_status()
      except httpx.HTTPError as exc:
        logger.warning("SerpAPI request failed for query %s: %s", q, exc)
        continue

      data = resp.json()
      for result in data.get("organic_results", []):
        url = result.get("link")
        if not url or url in seen_urls:
          continue
        seen_urls.add(url)
        candidates.append(url)

  if not candidates:
    return []

  # Apply relevance scoring and filtering.
  scored = []
  for url in candidates:
    if is_discarded_url(url):
      continue
    score = score_url_for_relevance(url)
    scored.append((score, url))

  if not scored:
    return []

  # Sort by score (desc), then by URL for stability.
  scored.sort(key=lambda s: (s[0], s[1]), reverse=True)

  # Deduplicate domains and cap to max_urls.
  selected: List[str] = []
  seen_domains = set()
  for _, url in scored:
    if len(selected) >= max_urls:
      break
    domain = urlparse(url).netloc or url
    if domain in seen_domains:
      continue
    seen_domains.add(domain)
    selected.append(url)

  return selected


async def find_play_store_app(
  company_name: str,
  api_key: str,
) -> Optional[str]:
  """
  Try to discover a Google Play Store app package ID for the given company.

  Strategy:
  - Use SerpAPI to search for a play.google.com app details URL
  - Extract the first valid `id=` package string
  - Return None on any failure or if no app is found
  """
  if not PLAYSTORE_ENABLED or not api_key or not company_name.strip():
    return None

  base = company_name.strip()
  queries = [
    f"\"{base}\" app site:play.google.com/store/apps/details",
    f"\"{base}\" android app",
  ]

  async with httpx.AsyncClient(timeout=10.0) as client:
    for q in queries:
      params = {"engine": "google", "q": q, "api_key": api_key}
      try:
        resp = await client.get("https://serpapi.com/search.json", params=params)
        resp.raise_for_status()
      except httpx.HTTPError as exc:
        logger.warning("SerpAPI Play Store discovery failed for %s: %s", q, exc)
        continue

      data = resp.json()
      for result in data.get("organic_results", []):
        url = result.get("link") or ""
        if "play.google.com/store/apps/details" not in (url or ""):
          continue
        # Extract package id query param
        parsed = urlparse(url)
        query = parsed.query or ""
        for part in query.split("&"):
          if part.startswith("id="):
            pkg = part.split("=", 1)[1].strip()
            if pkg:
              return pkg

  return None


async def fetch_page_html(url: str, client: httpx.AsyncClient) -> Optional[str]:
  """Fetch page HTML for a single URL, ignoring failures and non-HTML resources."""
  try:
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
  except httpx.HTTPError as exc:
    logger.warning("Failed to fetch %s: %s", url, exc)
    return None

  content_type = resp.headers.get("content-type", "").lower()
  if "text/html" not in content_type:
    # Skip PDFs, images, and other non-HTML resources.
    return None

  return resp.text


def extract_main_text(html: str, max_chars: int = 4000) -> Optional[str]:
  """
  Extract readable text from a page using trafilatura.
  Returns cleaned, truncated plain text or None if extraction fails.
  """
  if not html:
    return None

  try:
    text = trafilatura.extract(
      html,
      include_comments=False,
      include_tables=False,
      no_fallback=True,
    )
  except Exception as exc:
    logger.warning("trafilatura.extract failed: %s", exc)
    return None

  if not text:
    return None

  # Normalize whitespace to keep the prompt compact.
  cleaned = " ".join(text.split())
  return cleaned[:max_chars]


def extract_quotes_from_text(
  text: str,
  max_quotes: int = 3,
  max_chars: int = 200,
) -> List[str]:
  """Extract short representative snippets from page text."""
  if not text:
    return []

  # Very lightweight sentence splitting.
  sentences: List[str] = []
  current = []
  for ch in text:
    current.append(ch)
    if ch in ".!?":
      sentence = "".join(current).strip()
      current = []
      if len(sentence) >= 30:
        sentences.append(sentence)
  if current:
    tail = "".join(current).strip()
    if len(tail) >= 30:
      sentences.append(tail)

  quotes: List[str] = []
  for sent in sentences:
    snippet = sent.strip()
    if len(snippet) > max_chars:
      snippet = snippet[: max_chars - 1].rstrip() + "…"
    quotes.append(snippet)
    if len(quotes) >= max_quotes:
      break

  return quotes


async def collect_corpus_from_urls(
  urls: List[str],
  primary_company: str,
) -> Tuple[str, List[SourceInfo]]:
  """
  Fetch and extract content from each URL, then combine into a single corpus.
  Also returns per-source metadata and representative quotes.
  Only retains pages whose extracted text appears to be about the primary brand/domain.
  """
  if not urls:
    return "", []

  brand_tokens = build_brand_tokens(primary_company)

  async with httpx.AsyncClient(timeout=10.0) as client:
    tasks = [fetch_page_html(url, client) for url in urls]
    pages = await asyncio.gather(*tasks, return_exceptions=True)

  excerpts: List[str] = []
  sources: List[SourceInfo] = []
  for url, page in zip(urls, pages):
    if isinstance(page, Exception) or page is None:
      continue
    extracted = extract_main_text(page)
    if not extracted:
      continue

    # Drop generic context pages that never mention the primary brand/domain.
    text_lower = extracted.lower()
    if brand_tokens and not any(tok in text_lower for tok in brand_tokens):
      continue

    domain = urlparse(url).netloc or url
    kind = infer_source_kind(url)
    quotes = extract_quotes_from_text(extracted)

    sources.append(SourceInfo(url=url, domain=domain, kind=kind, quotes=quotes))
    excerpts.append(f"Source: {domain} ({kind}) - {url}\n\n{extracted}")

  if not excerpts:
    return "", []

  combined = "\n\n---\n\n".join(excerpts)

  # Keep the overall context to a reasonable size for the model.
  max_total_chars = 20000
  combined = combined[:max_total_chars]
  return combined, sources


def build_play_store_corpus(
  package_name: str,
  reviews_list: List[PlayStoreReview],
) -> Tuple[str, Optional[SourceInfo]]:
  """
  Turn processed Play Store reviews into a corpus block and a SourceInfo entry.

  The corpus favours complaints and feature gaps but keeps some positive signal.
  """
  if not reviews_list:
    return "", None

  lines: List[str] = []
  for r in reviews_list:
    snippet = r.text
    prefix = f"Rating: {r.rating}★"
    if r.app_version:
      prefix += f" (v{r.app_version})"
    if r.is_update_related:
      prefix += " [update-related]"
    if r.thumbs_up_count and r.thumbs_up_count > 0:
      prefix += f" [helpful: {r.thumbs_up_count}]"
    lines.append(f"{prefix} — {snippet}")

  block = (
    f"Source: Google Play Store reviews for package {package_name} (play_store)\n\n"
    + "\n\n".join(lines)
  )

  src = SourceInfo(
    url=f"https://play.google.com/store/apps/details?id={package_name}",
    domain="play.google.com",
    kind="play_store",
    quotes=[r.text for r in reviews_list[:5]],
  )
  return block, src


async def generate_insight_report(
  primary_company: str,
  competitor: Optional[str],
  corpus: str,
  sources: List[SourceInfo],
  analyzed_at: datetime,
) -> str:
  """Call the Claude (Anthropic) API to turn the combined corpus into a structured markdown report."""
  if not corpus.strip() or not sources:
    return (
      "## Limited public data\n\n"
      f"I was unable to extract enough relevant public web content about **{primary_company}**"
      f"{f' and **{competitor}**' if competitor else ''} to produce a meaningful analysis.\n\n"
      "There may be limited discussion online, or the available pages are not suitable for analysis.\n\n"
      "- Try a larger or more established competitor.\n"
      "- Try using a broader brand or product name.\n"
      "- Consider analyzing the company’s own website positioning instead.\n"
    )

  timestamp_str = analyzed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
  # Prepare a human-readable summary of sources and evidence quotes.
  source_lines: List[str] = []
  for src in sources:
    header = f"- {src.domain} — {src.kind} — {src.url}"
    if src.quotes:
      quotes_block = "\n".join(f'  - "{q}"' for q in src.quotes)
      source_lines.append(f"{header}\n{quotes_block}")
    else:
      source_lines.append(header)
  sources_block = "\n".join(source_lines)

  system_prompt = (
    "You are a senior product strategist and market research analyst. "
    "You analyze noisy user-generated content and extract clear, actionable, non-marketing insights "
    "for SaaS founders. You must not fabricate quotes or sources."
  )

  comparison_clause = (
    f" and its competitor **{competitor}**"
    if competitor
    else ""
  )

  user_prompt = f"""
You are analyzing public web discussions, reviews, and commentary about **{primary_company}**{comparison_clause}.

Timestamp (UTC): {timestamp_str}

The following sources were collected and pre-processed for you. Each source includes its
domain, rough type, URL, and up to a few short evidence quotes directly copied from the page.

Sources and evidence quotes:
{sources_block}

Corpus text (multiple sources, separated by ---):
\"\"\"text
{corpus}
\"\"\"

Using ONLY the information above, produce a concise but rich report in **markdown** with the following sections, in this order:

## Overall Sentiment
- 2–4 bullets summarizing the overall perception of {primary_company}{f' vs {competitor}' if competitor else ''}.
- Use plain, non-marketing language and be explicit about uncertainty if the data is thin.

## Key Strengths
- Bullet list of what users seem to like or praise.
- When possible, include 1–3 short supporting quotes from the evidence list above.

## Key Weaknesses
- Bullet list of notable shortcomings, frustrations, or gaps.
- When possible, include 1–3 short supporting quotes from the evidence list above.

## Common Complaints
- Bullet list focused on recurring or strong negative themes (support, reliability, pricing, UX, etc.).
- When possible, include 1–3 short supporting quotes from the evidence list above.

## Feature Requests / Unmet Needs
- Bullet list of requested features, missing capabilities, or unmet needs (if any).
- Be explicit if there is very little signal here.

## Opportunities for a New Competitor
- Concrete opportunities where a new product or competitor could outperform {primary_company}{f' or {competitor}' if competitor else ''}.
- Focus heavily on specific product and experience changes a new SaaS could implement that the existing product does not offer or does poorly (onboarding, integrations, workflows, pricing model, support, data, etc.).

{"## Competitive Comparison\n- Analyze overlap and differences between the two companies.\n- Include:\n  - Shared strengths\n  - Where the primary company outperforms the competitor\n  - Where the competitor outperforms the primary company\n  - Differentiation opportunities for the primary company\n  - Key reasons a buyer might choose one over the other\n- If there is clearly not enough evidence about the competitor to support a fair comparison, say so explicitly and avoid making up claims about them.\n" if competitor else ""}
## Sources Analyzed
- Start with a line like: `Sources analyzed: N (timestamp: {timestamp_str})`.
- Then list each source in the format: `domain.com — type — URL`.

Important rules:
- When you include quotes, ONLY use exact text from the evidence quotes provided above.
- Always attribute each quote to its domain, e.g. `"This tool is slow for large teams." — reddit.com`.
- Do not invent sources, URLs, or quotes.
- If the evidence is weak or ambiguous on a point, say so explicitly instead of guessing.
"""

  try:
    response = await anthropic_client.messages.create(
      model=ANTHROPIC_MODEL,
      max_tokens=2000,
      temperature=0.3,
      messages=[{"role": "user", "content": user_prompt}],
      system=system_prompt,
    )
  except Exception as exc:  # Broad by design for fast MVP error handling.
    raise RuntimeError(f"AI analysis failed: {exc}") from exc

  # Anthropic responses contain a list of content blocks; join any text segments.
  parts: List[str] = []
  for block in getattr(response, "content", []) or []:
    text = getattr(block, "text", None)
    if not text and isinstance(block, dict):
      text = block.get("text")
    if text:
      parts.append(text)

  content = "\n".join(parts).strip()
  return content or "Unable to generate a report from the provided data."


async def analyze_company(primary: str, competitor: Optional[str] = None) -> AnalysisResult:
  """
  End-to-end pipeline:
  SerpAPI search → URL filtering → fetch pages → extract text → LLM analysis.
  """
  if not SERPAPI_KEY:
    raise RuntimeError(
      "SERPAPI_KEY environment variable is missing. "
      "Set it to a valid SerpAPI key and try again."
    )

  primary_clean = primary.strip()
  competitor_clean = (competitor or "").strip() or None
  if not primary_clean:
    raise RuntimeError("Primary company query must not be empty.")

  cache_key: AnalysisCacheKey = (
    normalize_company_name(primary_clean),
    normalize_company_name(competitor_clean) if competitor_clean else None,
  )
  if cache_key in _analysis_cache:
    return _analysis_cache[cache_key]

  urls = await fetch_serp_urls(primary_clean, SERPAPI_KEY, competitor_clean)
  if not urls or len(urls) < 2:
    # Low-signal case: be explicit and avoid hallucinating.
    msg = (
      "## Limited public discussion\n\n"
      f"After filtering for relevant, high-signal sources, I could only find "
      f"{len(urls) if urls else 0} useful page(s) about **{primary_clean}**"
      f"{f' and **{competitor_clean}**' if competitor_clean else ''}.\n\n"
      "To get a stronger report:\n"
      "- Try a larger or more established competitor or market leader.\n"
      "- Try a broader brand or product name.\n"
      "- Consider analyzing the company’s own website positioning instead.\n"
    )
    result = AnalysisResult(
      report=msg,
      sources=[],
      analyzed_at=datetime.now(timezone.utc),
    )
    _analysis_cache[cache_key] = result
    return result

  corpus, sources = await collect_corpus_from_urls(urls, primary_clean)

  # Try to enrich with Play Store reviews (high-signal complaints and feature gaps).
  play_store_block = ""
  play_store_source: Optional[SourceInfo] = None
  if PLAYSTORE_ENABLED:
    try:
      pkg = await find_play_store_app(primary_clean, SERPAPI_KEY)
      if pkg:
        play_reviews = await fetch_play_store_reviews(pkg)
        play_store_block, play_src = build_play_store_corpus(pkg, play_reviews)
        play_store_source = play_src
    except Exception as exc:  # pragma: no cover - defensive
      logger.warning("Play Store enrichment failed for %s: %s", primary_clean, exc)

  if play_store_block:
    # Append Play Store reviews as an additional, high-priority block.
    if corpus:
      corpus = f"{play_store_block}\n\n---\n\n{corpus}"
    else:
      corpus = play_store_block
    if play_store_source:
      sources.append(play_store_source)
  analyzed_at = datetime.now(timezone.utc)
  report = await generate_insight_report(primary_clean, competitor_clean, corpus, sources, analyzed_at)

  result = AnalysisResult(report=report, sources=sources, analyzed_at=analyzed_at)
  _analysis_cache[cache_key] = result
  return result


async def generate_tailored_strategy(
  report: str,
  primary_company: str,
  competitor: Optional[str],
  product: Optional[ProductContext],
) -> Dict[str, Any]:
  """
  Call the LLM for a second, strictly-JSON pass that turns the base report
  plus the user's product context into a tailored strategy bundle.
  """
  if not product or not (product.one_liner or product.homepage_copy or product.landing_url):
    return {}

  comparison_clause = f" and its competitor {competitor}" if competitor else ""

  user_prompt = f"""
You are helping a founder position their product against **{primary_company}**{comparison_clause}.

First, here is a synthesized competitive insight report about the existing product(s):

\"\"\"markdown
{report}
\"\"\"

Now here is the founder's own product context. Use it to tailor all recommendations specifically to THEM:

- One-liner: {product.one_liner}
- Landing page URL (may be empty): {product.landing_url or "-"}
- Homepage copy (may be empty): {product.homepage_copy or "-"}
- Category: {product.category or "-"}
- Audience (who they sell to): {product.audience or "-"}

Using ONLY this information, return a single JSON object with the following top-level keys:

- "tailoredStrategy": object
- "underservedSegments": array
- "switchTriggers": array
- "messagingAngles": object
- "featureGapMap": array
- "blindSpots": array
- "strategicOptions": array
- "compareResults": object

The JSON structure:

{{
  "tailoredStrategy": {{
    "positioningAngle": "1-sentence positioning angle for THIS founder's product",
    "icp": "1–2 sentence ideal customer profile",
    "differentiators": ["3–5 short bullets"],
    "headlines": ["3 concise homepage headline options"],
    "subheadlines": ["3 concise homepage subheadline options"],
    "objections": [
      {{
        "objection": "short objection",
        "rebuttal": "short, specific rebuttal"
      }}
    ]
  }},
  "underservedSegments": [
    {{
      "name": "segment name",
      "pain": "1–2 line pain summary",
      "whyCompetitorFails": "why the current tool underserves them",
      "whatToBuildOrClaim": "what this founder should build or claim",
      "bestFor": ["teams", "students", "enterprise"]
    }}
  ],
  "switchTriggers": [
    {{
      "trigger": "event that makes users reconsider the tool",
      "why": "why it causes churn",
      "hook": "specific hook or offer to use"
    }}
  ],
  "messagingAngles": {{
    "Speed": [{{ "line": "short copy line", "useOn": ["hero","ads"] }}],
    "Trust": [{{ "line": "short copy line", "useOn": ["ads","cold email"] }}],
    "Simplicity": [],
    "Support": [],
    "Safety": []
  }},
  "featureGapMap": [
    {{
      "featureArea": "area or workflow",
      "competitorWeakness": "short description",
      "opportunity": "what the founder can do differently",
      "buildVsMarket": "build" | "market",
      "severity": "Low|Medium|High",
      "confidence": "Low|Medium|High"
    }}
  ],
  "blindSpots": [
    {{
      "topic": "what we don't know",
      "reason": "why the data is thin",
      "suggestedSources": ["G2", "Reddit", "App Store"]
    }}
  ],
  "strategicOptions": [
    {{
      "name": "strategy option name",
      "whoFor": "who this is best for",
      "whatToBuild": "concrete build focus",
      "whatToClaim": "narrative / claims",
      "risks": ["1–2 key risks"],
      "bestChannel": "SEO|paid|outbound|community"
    }}
  ],
  "compareResults": {{
    "overlapStrengths": [{{ "area": "short label", "detail": "1 line" }}],
    "uniqueAdvantages": [{{ "owner": "founder|competitor", "area": "short label", "detail": "1 line" }}],
    "biggestWedge": "1–2 sentence narrative wedge for the founder",
    "battlecard": "short markdown battlecard summary"
  }}
}}

Rules:
- Respond with **JSON only**. No markdown fences, no commentary.
- Keep all strings short, concrete, and free of hype language.
- If information is missing for a field, use an empty array [] or null.
"""

  try:
    response = await anthropic_client.messages.create(
      model=ANTHROPIC_MODEL,
      max_tokens=1800,
      temperature=0.4,
      messages=[{"role": "user", "content": user_prompt}],
      system="You are a precise JSON API. You return strictly valid JSON matching the requested schema.",
    )
  except Exception as exc:
    logger.exception("Tailored strategy generation failed: %s", exc)
    return {}

  parts: List[str] = []
  for block in getattr(response, "content", []) or []:
    text = getattr(block, "text", None)
    if not text and isinstance(block, dict):
      text = block.get("text")
    if text:
      parts.append(text)
  raw = "".join(parts).strip()
  if not raw:
    return {}

  try:
    data = json.loads(raw)
  except json.JSONDecodeError:
    logger.warning("Failed to decode tailored strategy JSON; raw content starts: %r", raw[:200])
    return {}

  if not isinstance(data, dict):
    return {}

  # Basic validation: keep only expected top-level keys.
  allowed_keys = {
    "tailoredStrategy",
    "underservedSegments",
    "switchTriggers",
    "messagingAngles",
    "featureGapMap",
    "blindSpots",
    "strategicOptions",
    "compareResults",
  }
  cleaned: Dict[str, Any] = {}
  for key in allowed_keys:
    if key in data:
      cleaned[key] = data[key]
  return cleaned


@app.get("/", response_class=HTMLResponse)
async def get_root(request: Request) -> HTMLResponse:
  """Serve the main HTML page with the input form."""
  return templates.TemplateResponse("index.html", {"request": request})


@app.get("/app", response_class=HTMLResponse)
async def get_app_page(request: Request) -> HTMLResponse:
  """Serve the analysis app UI."""
  return templates.TemplateResponse("app.html", {"request": request})


@app.post("/analyze")
async def post_analyze(payload: AnalyzeRequest = Body(...)) -> dict:
  """
  Accept a primary company/domain (and optional competitor) and return an AI-generated markdown report.
  """
  primary = (payload.query or "").strip()
  competitor = (payload.competitor or "").strip() if payload.competitor else None
  product = payload.product

  if not primary:
    raise HTTPException(status_code=400, detail="Primary company must not be empty.")

  try:
    result = await analyze_company(primary, competitor)
  except RuntimeError as exc:
    # Log full details server-side but return a clean JSON error to the user.
    logger.exception("Analysis failed for primary=%r, competitor=%r", primary, competitor)
    raise HTTPException(status_code=500, detail=str(exc)) from exc
  except Exception as exc:
    logger.exception("Unexpected error during analysis for primary=%r, competitor=%r", primary, competitor)
    raise HTTPException(
      status_code=500,
      detail="Unexpected error while analyzing this company. Please try again.",
    ) from exc

  sources_payload = [
    {"url": s.url, "domain": s.domain, "kind": s.kind}
    for s in result.sources
  ]
  structured = structured_report_payload(result.report, result.sources, result.analyzed_at)
  response: Dict[str, Any] = {
    **structured,
    "sources": sources_payload,
  }

  # Attach product context back to the client (for UI) and request tailored strategy.
  product_context_dict: Optional[Dict[str, Any]] = None
  if product:
    try:
      product_context_dict = product.dict()
    except ValidationError:
      product_context_dict = None

  if product_context_dict:
    response["productContext"] = product_context_dict
    try:
      strategy_bundle = await generate_tailored_strategy(
        report=result.report,
        primary_company=primary,
        competitor=competitor,
        product=product,
      )
      if strategy_bundle:
        response.update(strategy_bundle)
    except Exception:
      logger.exception("Failed to generate tailored strategy bundle.")

  return response


@app.post("/share")
async def create_share(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
  """
  Store the latest structured report payload in memory and return a token
  that can be used to share a read-only view.
  """
  token = uuid4().hex
  _shared_reports[token] = {
    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "data": payload,
  }
  return {"token": token}


@app.get("/share/{token}")
async def get_share(token: str) -> Dict[str, Any]:
  item = _shared_reports.get(token)
  if not item:
    raise HTTPException(status_code=404, detail="Share link not found.")
  return item["data"]


if __name__ == "__main__":
  import uvicorn

  port = int(os.getenv("PORT", "8000"))
  uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

