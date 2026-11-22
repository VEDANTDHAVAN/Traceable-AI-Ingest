import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import re

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.github_metrics import GitHubError, rate_delay
from ai_ingest.repository_links import canonical_repository_url
from ai_ingest.storage.sqlite import paper_identity
from ai_ingest.readme_cache import load_readme_cache

ARXIV_REFERENCE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/"
    r"(?:abs|pdf)/"
    r"(?P<identifier>"
    r"\d{4}\.\d{4,5}"
    r"|[A-Za-z][A-Za-z0-9.-]*/\d{7}"
    r")"
    r"(?:v\d+)?"
    r"(?:\.pdf)?"
    r"(?=$|[\s\]\[(){}<>\"',;:!?#.])",
    flags=re.IGNORECASE,
)


def referenced_papers(readme: str) -> set[str]:
    identities = set()

    for match in ARXIV_REFERENCE.finditer(readme):
        identifier = match.group("identifier")

        identities.add(
            paper_identity(
                f"https://arxiv.org/abs/{identifier}"
            )
        )

    return identities


def assess_association(
    row: dict,
    readme: str,
    *,
    paper_candidate_count: int,
    repository_candidate_count: int,
) -> tuple[bool, str]:
    github = row["github"]

    if "github_stars" not in github:
        return False, "METRICS_UNAVAILABLE"

    if not row.get("paper_link_evidence"):
        return False, "PAPER_LINK_EVIDENCE_MISSING"

    # Require explicit metadata, not an unknown fork status.
    if github.get("fork") is not False:
        return False, "FORK_OR_UNKNOWN_FORK_STATUS"

    if paper_candidate_count != 1:
        return False, "MULTIPLE_REPOSITORIES_FOR_PAPER"

    if repository_candidate_count != 1:
        return False, "REPOSITORY_LINKED_TO_MULTIPLE_PAPERS"

    references = referenced_papers(readme)

    if row["paper_id"] not in references:
        return False, "NO_MATCHING_README_REFERENCE"

    if references != {row["paper_id"]}:
        return False, "README_REFERENCES_MULTIPLE_PAPERS"

    return True, "RECIPROCAL_ARXIV_METADATA_README_LINK_V1"


async def fetch_readme(
    session: aiohttp.ClientSession,
    repository_url: str,
    raw_dir: Path,
) -> dict:
    canonical = canonical_repository_url(repository_url)

    if canonical is None:
        raise GitHubError("Invalid repository URL.")

    repository = canonical.removeprefix(
        "https://github.com/"
    )

    endpoint = (
        f"https://api.github.com/repos/{repository}/readme"
    )

    for attempt in range(4):
        await asyncio.sleep(1.0)

        try:
            async with session.get(
                endpoint,
                allow_redirects=False,
            ) as response:
                # Limit the response body retained in memory.
                chunks = []
                size = 0

                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)

                    if size > 2_000_000:
                        raise GitHubError(
                            "README response exceeds the "
                            "2 MB local safety limit."
                        )

                    chunks.append(chunk)

                raw = b"".join(chunks)
                status = response.status

                headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                }

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == 3:
                raise GitHubError(
                    "README network retries exhausted."
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

        evidence = {
            "endpoint": endpoint,
            "http_status": status,
            "observed_at": observed_at.isoformat(),
            "raw_path": str(raw_path.resolve()),
            "raw_sha256": digest,
        }

        await asyncio.to_thread(
            raw_path.with_suffix(".metadata.json").write_text,
            json.dumps(evidence, indent=2),
            encoding="utf-8",
        )

        if status == 200:
            try:
                payload = json.loads(raw)

                if payload.get("encoding") != "base64":
                    raise ValueError(
                        "Expected Base64 README content."
                    )

                compact = "".join(
                    payload["content"].split()
                )

                decoded = base64.b64decode(
                    compact,
                    validate=True,
                )

                text = decoded.decode("utf-8")

            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise GitHubError(
                    "README response could not be decoded."
                ) from exc

            return {
                "available": True,
                "text": text,
                "evidence": {
                    **evidence,
                    "readme_path": payload.get("path"),
                    "readme_git_sha": payload.get("sha"),
                    "readme_html_url": payload.get("html_url"),
                },
            }

        if status in {301, 302, 307, 308, 404, 410, 451}:
            return {
                "available": False,
                "text": "",
                "evidence": evidence,
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

        if is_rate_limit and attempt < 3:
            delay = max(
                rate_delay(headers, now=observed_at),
                60.0 * (2 ** attempt),
            )

            if delay > 300:
                raise GitHubError(
                    "GitHub requires a longer pause. "
                    f"Wait at least {int(delay) + 1} seconds."
                )

            await asyncio.sleep(
                delay + random.uniform(0.0, 1.0)
            )
            continue

        if status in {500, 502, 503, 504} and attempt < 3:
            await asyncio.sleep(
                random.uniform(1.0, 2.0 ** (attempt + 1))
            )
            continue

        raise GitHubError(
            f"README request failed with HTTP {status}."
        )

    raise GitHubError("README retry loop ended unexpectedly.")


async def verify(input_path: Path, output_dir: Path) -> dict:
    with input_path.open(encoding="utf-8") as handle:
        rows = [
            json.loads(line)
            for line in handle
            if line.strip()
        ]

    # Fail closed rather than silently inflate counts on duplicated input.
    pairs = {
        (row["paper_id"], row["candidate_repository_url"])
        for row in rows
    }

    if len(pairs) != len(rows):
        raise ValueError(
            "Duplicate paper/repository observations in input."
        )

    paper_counts = Counter(
        row["paper_id"] for row in rows
    )

    repository_counts = Counter(
        row["candidate_repository_url"] for row in rows
    )

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

    cache = load_readme_cache(output_dir)

    eligible_repositories = {
        row["candidate_repository_url"]
        for row in rows
        if "github_stars" in row["github"]
    }

    historical_readmes_available = len(
        eligible_repositories.intersection(cache)
    )

    fetched_readme_repositories = 0
    results = []

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
        connector=aiohttp.TCPConnector(limit=1),
    ) as session:
        for row in rows:
            repository = row["candidate_repository_url"]

            if "github_stars" not in row["github"]:
                accepted = False
                reason = "METRICS_UNAVAILABLE"
                readme_evidence = None

            else:
                if repository not in cache:
                    cache[repository] = await fetch_readme(
                        session,
                        repository,
                        raw_dir,
                    )

                    fetched_readme_repositories += 1

                readme = cache[repository]
                readme_evidence = readme["evidence"]

                if not readme["available"]:
                    accepted = False
                    reason = "README_UNAVAILABLE_OR_MOVED"
                else:
                    accepted, reason = assess_association(
                        row,
                        readme["text"],
                        paper_candidate_count=paper_counts[
                            row["paper_id"]
                        ],
                        repository_candidate_count=repository_counts[
                            repository
                        ],
                    )

            results.append(
                {
                    **row,
                    "association_verified": accepted,
                    "association_status": (
                        "ACCEPTED_BY_RULE"
                        if accepted
                        else "UNRESOLVED"
                    ),
                    "association_reason": reason,
                    "association_checked_at": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "readme_evidence": readme_evidence,
                }
            )

    output_path = run_dir / "associations.jsonl"
    temporary = run_dir / "associations.jsonl.tmp"

    with temporary.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(
                json.dumps(result, ensure_ascii=False) + "\n"
            )

    temporary.replace(output_path)

    reasons = Counter(
        row["association_reason"] for row in results
    )

    return {
        "candidate_associations": len(results),
        "accepted_by_reciprocal_link_rule": sum(
            row["association_verified"] for row in results
        ),
        "historical_readmes_available": historical_readmes_available,
        "fetched_readme_repositories": fetched_readme_repositories,
        "unresolved": sum(
            not row["association_verified"]
            for row in results
        ),
        "reason_counts": dict(reasons),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check reciprocal paper/repository links."
    )

    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a GitHub observations.jsonl file.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/associations"),
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            verify(args.input, args.output_dir)
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Association verification failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()