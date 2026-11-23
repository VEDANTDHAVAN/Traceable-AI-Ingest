"""Article text extraction, metadata recovery and stable content identities."""

from __future__ import annotations

import hashlib
import json
import re

from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ai_ingest.freshness import parse_publication


TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}

WHITESPACE = re.compile(r"\s+")


def canonical_url(value: str) -> str:
    """Canonicalize an article/job URL without changing its identity."""
    parsed = urlsplit(value.strip())

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be absolute http(s).")

    host = parsed.hostname.casefold()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path.rstrip("/") or "/"

    return urlunsplit(("https", host, path, "", ""))


def normalize_whitespace(value: str) -> str:
    return WHITESPACE.sub(" ", value).strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_identity(text: str) -> str:
    """Deduplicate near-identical article bodies.

    Whitespace and case are normalized before hashing so that syndicated
    copies with different markup collapse to one identity.
    """
    return sha256_text(normalize_whitespace(text).casefold())


def _meta(soup: BeautifulSoup, *, property_name: str | None = None, name: str | None = None) -> str | None:
    if property_name:
        tag = soup.find("meta", attrs={"property": property_name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def extract_json_ld_dates(soup: BeautifulSoup) -> list[str]:
    dates: list[str] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = script.string or script.get_text()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue

        queue = data if isinstance(data, list) else [data]
        while queue:
            item = queue.pop()
            if isinstance(item, dict):
                for key in ("datePublished", "dateCreated"):
                    value = item.get(key)
                    if isinstance(value, str):
                        dates.append(value)
                queue.extend(item.values())
            elif isinstance(item, list):
                queue.extend(item)

    return dates


def extract_article(html: str, *, fallback_title: str | None = None) -> dict:
    """Recover title, canonical URL, publication date and body text.

    Missing metadata stays None.  The caller decides whether a date recovered
    here is trustworthy enough for the freshness gate.
    """
    soup = BeautifulSoup(html, "html.parser")

    title = (
        _meta(soup, property_name="og:title")
        or _meta(soup, name="twitter:title")
        or (soup.title.get_text(strip=True) if soup.title else None)
        or fallback_title
    )

    canonical = None
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        canonical = str(link["href"]).strip()
    canonical = canonical or _meta(soup, property_name="og:url")

    date_candidates = [
        _meta(soup, property_name="article:published_time"),
        _meta(soup, name="article:published_time"),
        _meta(soup, name="date"),
        _meta(soup, name="pubdate"),
        _meta(soup, name="publish-date"),
        _meta(soup, name="parsely-pub-date"),
        _meta(soup, name="sailthru.date"),
    ]
    date_candidates.extend(extract_json_ld_dates(soup))
    date_candidates.append(_time_datetime(soup))

    published = None
    for candidate in date_candidates:
        if not candidate:
            continue
        parsed = parse_publication(candidate)
        if parsed is not None:
            published = parsed
            break

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body
    paragraphs: list[str] = []
    if root is not None:
        for paragraph in root.find_all(["p", "h2", "h3", "li"]):
            text = normalize_whitespace(paragraph.get_text(" ", strip=True))
            if len(text) >= 40:
                paragraphs.append(text)

    body = "\n\n".join(paragraphs)
    if not body and root is not None:
        body = normalize_whitespace(root.get_text(" ", strip=True))

    return {
        "title": normalize_whitespace(title) if title else None,
        "canonical_url": canonical,
        "published_at": published,
        "text": body,
        "extraction_status": "FULL_TEXT" if len(body) >= 200 else "FEED_SUMMARY",
    }


def _time_datetime(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all("time"):
        value = tag.get("datetime") or tag.get_text(strip=True)
        if value:
            return str(value).strip()
    return None


def date_from_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
