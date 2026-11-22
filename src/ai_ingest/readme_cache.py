import base64
import hashlib
import json

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_ingest.repository_links import canonical_repository_url


def read_cached_readme(
    evidence: dict,
    *,
    repository_url: str,
    now: datetime,
    max_age: timedelta,
) -> dict | None:
    try:
        canonical = canonical_repository_url(repository_url)

        if canonical is None:
            return None

        repository = canonical.removeprefix(
            "https://github.com/"
        )

        expected_endpoint = (
            f"https://api.github.com/repos/{repository}/readme"
        )

        if evidence["endpoint"].lower() != expected_endpoint.lower():
            return None

        if evidence["http_status"] != 200:
            return None

        observed_at = datetime.fromisoformat(
            evidence["observed_at"].replace("Z", "+00:00")
        )

        if observed_at.tzinfo is None:
            return None

        age = now - observed_at

        if age < timedelta(0) or age > max_age:
            return None

        raw_path = Path(evidence["raw_path"])

        if raw_path.stat().st_size > 2_000_000:
            return None

        raw = raw_path.read_bytes()

        if hashlib.sha256(raw).hexdigest() != evidence["raw_sha256"]:
            return None

        payload = json.loads(raw)

        if payload["encoding"] != "base64":
            return None

        compact = "".join(payload["content"].split())

        text = base64.b64decode(
            compact,
            validate=True,
        ).decode("utf-8")

        return {
            "available": True,
            "text": text,
            # Preserve the actual observation time.
            "evidence": dict(evidence),
        }

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return None


def load_readme_cache(
    history_dir: Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, dict]:
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive.")

    now = now or datetime.now(timezone.utc)
    cache = {}

    for path in sorted(history_dir.rglob("associations.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue

                row = json.loads(line)

                repository = canonical_repository_url(
                    row["candidate_repository_url"]
                )

                if repository is None:
                    continue

                evidence = row.get("readme_evidence")

                if not evidence:
                    continue

                cached = read_cached_readme(
                    evidence,
                    repository_url=repository,
                    now=now,
                    max_age=max_age,
                )

                if cached is None:
                    continue

                previous = cache.get(repository)

                if previous is not None:
                    previous_time = datetime.fromisoformat(
                        previous["evidence"]["observed_at"].replace(
                            "Z", "+00:00"
                        )
                    )

                    incoming_time = datetime.fromisoformat(
                        cached["evidence"]["observed_at"].replace(
                            "Z", "+00:00"
                        )
                    )

                    if incoming_time <= previous_time:
                        continue

                cache[repository] = cached

    return cache