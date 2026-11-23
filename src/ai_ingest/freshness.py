"""Publication-date normalization and the 24-hour freshness gate.

Every path returns an explicit status.  A missing or ambiguous date is never
guessed; the caller must quarantine the item when freshness is UNKNOWN.
"""

from __future__ import annotations

import re

from datetime import datetime, timedelta, timezone
from typing import Literal

from dateutil import parser as date_parser


Freshness = Literal["FRESH_24H", "STALE", "FUTURE", "UNKNOWN"]

FRESH_WINDOW = timedelta(hours=24)

# A small forward allowance covers clock skew between publisher and host.
MAX_FUTURE_SKEW = timedelta(minutes=5)

_RELATIVE = re.compile(
    r"^\s*(?:about\s+|around\s+|~)?(\d+|an?|one)\s*"
    r"(second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\s*$",
    re.IGNORECASE,
)

_WORD_AMOUNTS = {"a": 1, "an": 1, "one": 1}


def _relative(value: str, now: datetime) -> datetime | None:
    text = value.strip().casefold()

    if text in {"just now", "now", "right now"}:
        return now

    if text in {"yesterday", "yesterday at midnight"}:
        return (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    match = _RELATIVE.match(text)
    if match is None:
        return None

    amount_text, unit = match.groups()
    amount = _WORD_AMOUNTS.get(amount_text, None)
    if amount is None:
        amount = int(amount_text)

    factors = {
        "second": 1,
        "sec": 1,
        "minute": 60,
        "min": 60,
        "hour": 3600,
        "hr": 3600,
        "day": 86400,
        "week": 604800,
        "month": 2_592_000,
        "year": 31_536_000,
    }

    return now - timedelta(seconds=amount * factors[unit])


def parse_publication(value: object, *, now: datetime | None = None) -> datetime | None:
    """Parse an absolute or relative publication date into aware UTC.

    Returns None rather than inventing a date.  Naive timestamps are treated as
    UTC only when the source is documented as UTC; callers pass those values
    explicitly (Unix epochs are inherently UTC).
    """
    current = now or datetime.now(timezone.utc)

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Unix epoch seconds, always UTC.
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()

        relative = _relative(text, current)
        if relative is not None:
            parsed = relative
        else:
            try:
                parsed = date_parser.parse(text)
            except (ValueError, OverflowError, TypeError):
                return None
    else:
        return None

    if parsed.tzinfo is None:
        # No timezone: without a documented UTC source this is ambiguous.
        return None

    return parsed.astimezone(timezone.utc)


def classify_freshness(
    published: datetime | None,
    *,
    now: datetime | None = None,
) -> Freshness:
    if published is None:
        return "UNKNOWN"

    current = now or datetime.now(timezone.utc)
    age = current - published

    if age < -MAX_FUTURE_SKEW:
        return "FUTURE"

    if age <= FRESH_WINDOW:
        return "FRESH_24H"

    return "STALE"
