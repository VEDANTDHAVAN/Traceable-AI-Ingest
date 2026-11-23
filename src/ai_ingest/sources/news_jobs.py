"""News-feed and job-board adapters.

Each adapter declares its observed endpoint, the fields actually present and
its access policy.  Dates come from structured fields (RSS/Atom, JSON, epoch)
and are never inferred from page position or fetch time.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from typing import Callable

import feedparser

from ai_ingest.content import normalize_whitespace
from ai_ingest.freshness import parse_publication


NEWS_FEEDS: dict[str, dict] = {
    "techcrunch": {
        "name": "TechCrunch — Artificial Intelligence",
        "feed_url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "homepage": "https://techcrunch.com/category/artificial-intelligence/",
        "policy": "techcrunch",
        "domain": "techcrunch.com",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
    "theverge": {
        "name": "The Verge — AI",
        "feed_url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "homepage": "https://www.theverge.com/ai-artificial-intelligence",
        "policy": "theverge",
        "domain": "theverge.com",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
    "mitnews": {
        "name": "MIT News — Artificial Intelligence",
        "feed_url": "https://news.mit.edu/rss/topic/artificial-intelligence2",
        "homepage": "https://news.mit.edu/topic/artificial-intelligence2",
        "policy": "mitnews",
        "domain": "news.mit.edu",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
    "marktechpost": {
        "name": "MarkTechPost",
        "feed_url": "https://www.marktechpost.com/feed/",
        "homepage": "https://www.marktechpost.com/",
        "policy": "marktechpost",
        "domain": "marktechpost.com",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
    "analyticsindia": {
        "name": "Analytics India Magazine",
        "feed_url": "https://analyticsindiamag.com/feed/",
        "homepage": "https://analyticsindiamag.com/",
        "policy": "analyticsindia",
        "domain": "analyticsindiamag.com",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
    "siliconangle": {
        "name": "SiliconANGLE",
        "feed_url": "https://siliconangle.com/feed/",
        "homepage": "https://siliconangle.com/",
        "policy": "siliconangle",
        "domain": "siliconangle.com",
        "license_note": "Publisher RSS feed; headlines, links and summaries only.",
    },
}


AI_TERMS = (
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "generative ai",
    "genai",
    "large language model",
    "llm",
    "neural network",
    "computer vision",
    "natural language processing",
    "reinforcement learning",
    "ai model",
    "ai research",
    "ai startup",
    "foundation model",
    "agentic",
    "mlops",
    "chatbot",
    "prompt engineering",
    "transformer model",
)

AI_TOKEN = re.compile(
    r"\b(?:ai|ml|llm|llms|genai|nlp|gpt|chatbot)\b",
    re.IGNORECASE,
)


def ai_match_terms(*values: str | None) -> list[str]:
    """Return only the AI terms actually present in the supplied text."""
    text = " ".join(value for value in values if value).casefold()
    matched = [term for term in AI_TERMS if term in text]
    if AI_TOKEN.search(text):
        matched.append("ai-token")
    return sorted(set(matched))


def is_ai_related(*values: str | None) -> bool:
    return bool(ai_match_terms(*values))


def feed_entries(body: bytes) -> list[dict]:
    parsed = feedparser.parse(body)

    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"Feed could not be parsed: {parsed.bozo_exception}")

    entries: list[dict] = []
    for entry in parsed.entries:
        published_struct = entry.get("published_parsed")
        published = None
        if published_struct:
            published = dt.datetime(
                *published_struct[:6], tzinfo=dt.timezone.utc
            )

        summary = entry.get("summary") or ""
        content_blocks = entry.get("content") or []
        content_text = " ".join(
            block.get("value", "") for block in content_blocks if isinstance(block, dict)
        )

        entries.append(
            {
                "title": normalize_whitespace(entry.get("title") or ""),
                "link": (entry.get("link") or "").strip(),
                "published": published,
                "published_raw": entry.get("published"),
                "summary": summary,
                "content_text": content_text,
                "author": entry.get("author"),
            }
        )

    return entries


def _first(value: object, keys: tuple[str, ...]) -> object:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) not in (None, ""):
            return value[key]
    return None


def _parse_job_date(kind: str, row: dict) -> dt.datetime | None:
    if kind == "ashby":
        return parse_publication(row.get("publishedAt"))
    if kind == "greenhouse":
        return parse_publication(row.get("first_published") or row.get("updated_at"))
    if kind == "jobicy":
        return parse_publication(row.get("pubDate"))
    if kind == "arbeitnow":
        return parse_publication(row.get("created_at"))
    if kind == "himalayas":
        return parse_publication(row.get("pubDate"))
    if kind == "remoteok":
        return parse_publication(row.get("date") or row.get("epoch"))
    if kind == "remotive":
        return parse_publication(row.get("publication_date"))
    return None


ROLE_FAMILIES = {
    "engineering": "Engineering",
    "software": "Engineering",
    "infrastructure": "Engineering",
    "infra": "Engineering",
    "research": "Research",
    "data": "Data",
    "machine learning": "Data",
    "product": "Product",
    "design": "Design",
    "marketing": "Marketing",
    "sales": "Sales",
    "operations": "Operations",
    "security": "Security",
    "people": "People",
    "finance": "Finance",
    "legal": "Legal",
    "support": "Support",
}


def role_family(department: str | None, title: str | None) -> str | None:
    """Map only explicit source categories; never infer from seniority."""
    for value in (department, title):
        if not value:
            continue
        lowered = value.casefold()
        for needle, family in ROLE_FAMILIES.items():
            if needle in lowered:
                return family
    return None


def _remote_flag(kind: str, row: dict) -> bool | None:
    if kind == "ashby":
        value = row.get("isRemote")
        if isinstance(value, bool):
            return value
        workplace = str(row.get("workplaceType") or "").casefold()
        if workplace == "remote":
            return True
        if workplace in {"onsite", "hybrid"}:
            return False
        return None

    if kind == "greenhouse":
        # The API has no remote flag; absence is unknown, not False.
        return None

    if kind == "jobicy":
        geo = str(row.get("jobGeo") or "").casefold()
        if "remote" in geo or geo in {"anywhere", "global"}:
            return True
        return None

    if kind == "arbeitnow":
        value = row.get("remote")
        return value if isinstance(value, bool) else None

    if kind == "himalayas":
        restrictions = row.get("locationRestrictions")
        if restrictions == ["Anywhere"] or row.get("timezoneRestrictions"):
            return True
        return None

    if kind == "remoteok":
        return True

    if kind == "remotive":
        location = str(row.get("candidate_required_location") or "")
        return True if location else None

    return None


def _job_from_row(kind: str, row: dict) -> dict | None:
    if kind == "ashby":
        url = row.get("jobUrl")
        title = row.get("title")
        company = None  # Company comes from the board, not the row.
        description = row.get("descriptionPlain") or ""
        department = row.get("department") or row.get("team")
    elif kind == "greenhouse":
        url = row.get("absolute_url")
        title = row.get("title")
        company = row.get("company_name")
        description = ""
        department = _first(row, ("departments",))
        if not isinstance(department, str):
            department = None
    elif kind == "jobicy":
        url = row.get("url")
        title = row.get("jobTitle")
        company = row.get("companyName")
        description = row.get("jobDescription") or row.get("jobExcerpt") or ""
        industry = row.get("jobIndustry")
        department = industry[0] if isinstance(industry, list) and industry else industry
    elif kind == "arbeitnow":
        url = row.get("url")
        title = row.get("title")
        company = row.get("company_name")
        description = row.get("description") or ""
        tags = row.get("tags") or []
        department = " ".join(tags) if isinstance(tags, list) else str(tags)
    elif kind == "himalayas":
        url = row.get("applicationLink") or row.get("guid")
        title = row.get("title")
        company = row.get("companyName")
        description = row.get("description") or row.get("excerpt") or ""
        categories = row.get("categories")
        department = (
            " ".join(categories)
            if isinstance(categories, list)
            else str(categories or "")
        )
    elif kind == "remoteok":
        url = row.get("url") or row.get("apply_url")
        title = row.get("position")
        company = row.get("company")
        description = row.get("description") or ""
        tags = row.get("tags") or []
        department = " ".join(tags) if isinstance(tags, list) else str(tags)
    elif kind == "remotive":
        url = row.get("url")
        title = row.get("title")
        company = row.get("company_name")
        description = row.get("description") or ""
        department = " ".join(
            [str(row.get("category") or "")]
            + [str(tag) for tag in (row.get("tags") or [])]
        )
    else:
        return None

    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    if not isinstance(title, str) or not title.strip():
        return None

    return {
        "url": url.strip(),
        "title": normalize_whitespace(title),
        "company": company if isinstance(company, str) and company.strip() else None,
        "department": department if isinstance(department, str) else None,
        "description": description,
        "published_at": _parse_job_date(kind, row),
        "is_remote": _remote_flag(kind, row),
        "raw": row,
    }


def parse_job_payload(kind: str, body: bytes) -> list[dict]:
    payload = json.loads(body)

    if kind in {"ashby", "jobicy", "himalayas"}:
        rows = payload.get("jobs") if isinstance(payload, dict) else None
    elif kind == "arbeitnow":
        rows = payload.get("data") if isinstance(payload, dict) else None
    elif kind == "greenhouse":
        rows = payload.get("jobs") if isinstance(payload, dict) else None
    elif kind in {"remoteok", "remotive"}:
        rows = payload if isinstance(payload, list) else payload.get("jobs")
    else:
        rows = None

    if not isinstance(rows, list):
        raise ValueError("Unexpected job payload envelope.")

    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Remote OK includes a legal notice as its first element.
        if row.get("legal"):
            continue
        job = _job_from_row(kind, row)
        if job is not None:
            parsed.append(job)
    return parsed


JOB_BOARDS: dict[str, dict] = {
    "openai_careers": {
        "name": "OpenAI Careers",
        "kind": "ashby",
        "url": "https://api.ashbyhq.com/posting-api/job-board/openai",
        "homepage": "https://openai.com/careers/",
        "company": "OpenAI",
        "policy": "ashby",
        "domain": "api.ashbyhq.com",
        "access": "Official published Ashby job-board API.",
    },
    "cohere_careers": {
        "name": "Cohere Careers",
        "kind": "ashby",
        "url": "https://api.ashbyhq.com/posting-api/job-board/cohere",
        "homepage": "https://cohere.com/careers",
        "company": "Cohere",
        "policy": "ashby",
        "domain": "api.ashbyhq.com",
        "access": "Official published Ashby job-board API.",
    },
    "anthropic_careers": {
        "name": "Anthropic Careers",
        "kind": "greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs",
        "homepage": "https://www.anthropic.com/careers",
        "company": "Anthropic",
        "policy": "greenhouse",
        "domain": "boards-api.greenhouse.io",
        "access": "Official published Greenhouse job-board API.",
    },
    "jobicy": {
        "name": "Jobicy",
        "kind": "jobicy",
        "url": "https://jobicy.com/api/v2/remote-jobs?count=100&tag=ai",
        "homepage": "https://jobicy.com/",
        "company": None,
        "policy": "jobicy",
        "domain": "jobicy.com",
        "access": "Public JSON API documented by Jobicy.",
    },
    "arbeitnow": {
        "name": "Arbeitnow",
        "kind": "arbeitnow",
        "url": "https://www.arbeitnow.com/api/job-board-api",
        "homepage": "https://www.arbeitnow.com/",
        "company": None,
        "policy": "arbeitnow",
        "domain": "www.arbeitnow.com",
        "access": "Public JSON job-board API.",
    },
    "himalayas": {
        "name": "Himalayas",
        "kind": "himalayas",
        "url": "https://himalayas.app/jobs/api?limit=100",
        "homepage": "https://himalayas.app/",
        "company": None,
        "policy": "himalayas",
        "domain": "himalayas.app",
        "access": "Public JSON job API.",
    },
    "remoteok": {
        "name": "Remote OK",
        "kind": "remoteok",
        "url": "https://remoteok.com/api",
        "homepage": "https://remoteok.com/",
        "company": None,
        "policy": "remoteok",
        "domain": "remoteok.com",
        "access": "Public API; publisher requires attribution and a follow link.",
    },
    "remotive": {
        "name": "Remotive",
        "kind": "remotive",
        "url": "https://remotive.com/api/remote-jobs?limit=500",
        "homepage": "https://remotive.com/",
        "company": None,
        "policy": "remotive",
        "domain": "remotive.com",
        "access": "Public JSON API.",
    },
}


def board_company(kind: str, row: dict, board: dict, job: dict) -> str | None:
    if board.get("company"):
        return str(board["company"])
    return job.get("company")


def discount_duplicate_titles(jobs: list[dict]) -> list[dict]:
    """Remove exact duplicate listing URLs inside one payload."""
    seen: set[str] = set()
    unique = []
    for job in jobs:
        key = job["url"].rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def summarize_boards() -> dict:
    return {
        "news": {
            key: {"name": value["name"], "feed_url": value["feed_url"]}
            for key, value in NEWS_FEEDS.items()
        },
        "jobs": {
            key: {
                "name": value["name"],
                "url": value["url"],
                "access": value["access"],
            }
            for key, value in JOB_BOARDS.items()
        },
    }


__all__ = [
    "AI_TERMS",
    "JOB_BOARDS",
    "NEWS_FEEDS",
    "ai_match_terms",
    "board_company",
    "discount_duplicate_titles",
    "feed_entries",
    "is_ai_related",
    "parse_job_payload",
    "role_family",
    "summarize_boards",
]


Parser = Callable[[str, bytes], list[dict]]
