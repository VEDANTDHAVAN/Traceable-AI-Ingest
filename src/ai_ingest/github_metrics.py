import argparse
import asyncio
import hashlib
import json
import os
import random
import time

from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.repository_links import canonical_repository_url
from ai_ingest.sources.arxiv import retry_after_seconds


class GitHubError(RuntimeError):
    """GitHub metadata could not be fetched or validated."""


def validate_metadata(payload: dict) -> dict:
    """Validate fields used by our downstream enrichment."""
    full_name = payload.get("full_name")
    html_url = payload.get("html_url")
    stars = payload.get("stargazers_count")

    if not isinstance(full_name, str) or not full_name.strip():
        raise GitHubError("Missing repository full_name.")

    if not isinstance(html_url, str):
        raise GitHubError("Missing repository html_url.")

    repository_url = canonical_repository_url(html_url)

    if repository_url is None:
        raise GitHubError("Invalid repository html_url.")

    expected_url = canonical_repository_url(
        f"https://github.com/{full_name}"
    )

    if expected_url != repository_url:
        raise GitHubError("Repository name and URL disagree.")

    if type(stars) is not int or stars < 0:
        raise GitHubError("Invalid stargazers_count.")

    return {
        "repository_url": repository_url,
        "full_name": full_name,
        "github_stars": stars,
        "description": payload.get("description"),
        "homepage": payload.get("homepage"),
        "default_branch": payload.get("default_branch"),
        "archived": payload.get("archived"),
        "fork": payload.get("fork"),
    }


def rate_delay(
    headers: dict,
    *,
    now: datetime,
) -> float:
    headers = {
        key.lower(): value
        for key, value in headers.items()
    }

    retry_after = retry_after_seconds(
        headers.get("retry-after"),
        now=now,
    )

    delays = []

    if retry_after is not None:
        delays.append(retry_after)

    if headers.get("x-ratelimit-remaining") == "0":
        try:
            reset = float(headers["x-ratelimit-reset"])
            delays.append(
                max(0.0, reset - now.timestamp()) + 1.0
            )
        except (KeyError, TypeError, ValueError):
            pass

    return max(delays) if delays else 60.0


def read_candidates(path: Path) -> list[dict]:
    candidates = []

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue

            candidate = json.loads(line)
            original = candidate["repository_url"]
            normalized = canonical_repository_url(original)

            if normalized is None:
                raise ValueError(
                    f"Invalid candidate repository: {original}"
                )

            candidate["repository_url"] = normalized
            candidates.append(candidate)

    return candidates


async def fetch_repository(
    session: aiohttp.ClientSession,
    repository_url: str,
    raw_dir: Path,
) -> dict:
    canonical = canonical_repository_url(repository_url)

    if canonical is None:
        raise GitHubError("Invalid candidate repository URL.")

    owner_and_repo = canonical.removeprefix(
        "https://github.com/"
    )

    endpoint = (
        f"https://api.github.com/repos/{owner_and_repo}"
    )

    for attempt in range(4):
        # Sequential requests, including retries.
        await asyncio.sleep(1.0)

        try:
            async with session.get(
                endpoint,
                allow_redirects=False,
            ) as response:
                raw = await response.read()
                status = response.status

                headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                }

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == 3:
                raise GitHubError(
                    f"Network retries exhausted for {canonical}"
                ) from exc

            await asyncio.sleep(
                random.uniform(1.0, 2.0 ** (attempt + 1))
            )
            continue

        observed_at = datetime.now(timezone.utc)
        digest = hashlib.sha256(raw).hexdigest()

        filename = (
            observed_at.strftime("%Y%m%dT%H%M%S%fZ")
            + "-"
            + digest[:12]
            + ".json"
        )

        raw_path = raw_dir / filename

        await asyncio.to_thread(
            raw_path.write_bytes,
            raw,
        )

        # Store response metadata, not request headers or credentials.
        evidence_metadata = {
            "endpoint": endpoint,
            "http_status": status,
            "observed_at": observed_at.isoformat(),
            "raw_sha256": digest,
            "rate_limit_remaining": headers.get(
                "x-ratelimit-remaining"
            ),
            "rate_limit_reset": headers.get(
                "x-ratelimit-reset"
            ),
        }

        await asyncio.to_thread(
            raw_path.with_suffix(".metadata.json").write_text,
            json.dumps(evidence_metadata, indent=2),
            encoding="utf-8",
        )

        if status == 200:
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitHubError(
                    "GitHub returned invalid JSON."
                ) from exc

            if not isinstance(payload, dict):
                raise GitHubError(
                    "Expected a repository JSON object."
                )

            metadata = validate_metadata(payload)

            return {
                **metadata,
                "requested_repository_url": canonical,
                "github_metrics_collected_at": (
                    observed_at.isoformat()
                ),
                "raw_path": str(raw_path.resolve()),
                "raw_sha256": digest,
                "api_endpoint": endpoint,
            }

        # Do not silently follow repository moves or redirects.
        if status in {301, 302, 307, 308, 404, 410, 451}:
            return {
                "requested_repository_url": canonical,
                "fetch_status": "UNAVAILABLE_OR_MOVED",
                "http_status": status,
                "raw_path": str(raw_path.resolve()),
                "raw_sha256": digest,
            }

        is_rate_limit = (
            status == 429
            or (
                status == 403
                and (
                    headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in headers
                    or b"rate limit" in raw.lower()
                )
            )
        )

        if is_rate_limit:
            if attempt == 3:
                raise GitHubError(
                    "GitHub rate-limit retries exhausted."
                )

            delay = max(
                rate_delay(
                    headers,
                    now=observed_at,
                ),
                60.0 * (2 ** attempt),
            )

            # Avoid leaving a local command sleeping for an hour.
            # Abort the batch instead of continuing with other repos.
            if delay > 300:
                raise GitHubError(
                    "GitHub requests require a longer pause. "
                    f"Wait at least {int(delay) + 1} seconds "
                    "before rerunning."
                )

            await asyncio.sleep(
                delay + random.uniform(0.0, 1.0)
            )
            continue

        if status in {500, 502, 503, 504}:
            if attempt < 3:
                await asyncio.sleep(
                    random.uniform(1.0, 2.0 ** (attempt + 1))
                )
                continue

        raise GitHubError(
            f"GitHub HTTP {status} for {canonical}; "
            f"raw response saved at {raw_path}"
        )

    raise GitHubError("Retry loop ended unexpectedly.")


async def enrich(
    input_path: Path,
    output_dir: Path,
) -> dict:
    candidates = read_candidates(input_path)

    run_id = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    run_dir = output_dir / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-intelligence-ingestion/0.1",
    }

    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if token:
        headers["Authorization"] = f"Bearer {token}"

    repositories = sorted(
        {item["repository_url"] for item in candidates}
    )

    observations = {}

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=1),
    ) as session:
        for repository_url in repositories:
            observations[repository_url] = await fetch_repository(
                session,
                repository_url,
                raw_dir,
            )

    output_path = run_dir / "observations.jsonl"
    temporary = run_dir / "observations.jsonl.tmp"

    with temporary.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            observation = observations[
                candidate["repository_url"]
            ]

            row = {
                "paper_id": candidate["paper_id"],
                "candidate_repository_url": (
                    candidate["repository_url"]
                ),
                "association_verified": False,
                "association_status": "PENDING_REVIEW",
                "paper_link_evidence": candidate["evidence"],
                "github": observation,
            }

            handle.write(
                json.dumps(row, ensure_ascii=False) + "\n"
            )

    temporary.replace(output_path)

    return {
        "candidate_associations": len(candidates),
        "unique_repositories_requested": len(repositories),
        "repositories_with_metrics": sum(
            "github_stars" in item
            for item in observations.values()
        ),
        "verified_paper_associations": 0,
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub metadata for paper candidates."
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path(
            "data/arxiv/repository_candidates.jsonl"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/github"),
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            enrich(args.input, args.output_dir)
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"GitHub enrichment failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()