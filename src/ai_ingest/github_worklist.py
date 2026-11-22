import argparse
import hashlib
import json

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_ingest.github_metrics import validate_metadata
from ai_ingest.repository_links import canonical_repository_url


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [
            json.loads(line)
            for line in handle
            if line.strip()
        ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False) + "\n"
            )


def pair_key(row: dict) -> tuple[str, str]:
    return (
        row["paper_id"],
        row["candidate_repository_url"],
    )


def reusable_metrics(
    github: dict,
    *,
    now: datetime,
    max_age: timedelta,
) -> bool:
    """Require fresh metrics and intact supporting raw evidence."""
    try:
        observed_at = datetime.fromisoformat(
            github["github_metrics_collected_at"].replace(
                "Z", "+00:00"
            )
        )

        if observed_at.tzinfo is None:
            return False

        age = now - observed_at

        if age < timedelta(0) or age > max_age:
            return False

        raw_path = Path(github["raw_path"])
        raw = raw_path.read_bytes()

        if hashlib.sha256(raw).hexdigest() != github["raw_sha256"]:
            return False

        payload = json.loads(raw)
        validated = validate_metadata(payload)

        repository_url = canonical_repository_url(
            github["repository_url"]
        )

        return (
            repository_url == validated["repository_url"]
            and github["github_stars"] == validated["github_stars"]
            and type(github["github_stars"]) is int
        )

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
    ):
        return False


def find_cached_metrics(
    history_dir: Path,
    *,
    now: datetime,
    max_age: timedelta,
) -> dict[str, dict]:
    cache = {}

    for path in sorted(history_dir.rglob("observations.jsonl")):
        for row in read_jsonl(path):
            github = row.get("github", {})

            if not reusable_metrics(
                github,
                now=now,
                max_age=max_age,
            ):
                continue

            repository = canonical_repository_url(
                github["repository_url"]
            )

            previous = cache.get(repository)

            if previous is None:
                cache[repository] = github
                continue

            current_time = datetime.fromisoformat(
                github["github_metrics_collected_at"].replace(
                    "Z", "+00:00"
                )
            )

            previous_time = datetime.fromisoformat(
                previous["github_metrics_collected_at"].replace(
                    "Z", "+00:00"
                )
            )

            if current_time > previous_time:
                cache[repository] = github

    return cache


def build_worklist(
    candidates: list[dict],
    cache: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    cached_rows = []
    pending = []
    seen = set()

    for candidate in candidates:
        repository = canonical_repository_url(
            candidate["repository_url"]
        )

        if repository is None:
            raise ValueError("Invalid candidate repository URL.")

        key = (candidate["paper_id"], repository)

        if key in seen:
            raise ValueError("Duplicate candidate pair.")

        seen.add(key)

        if repository in cache:
            cached_rows.append(
                {
                    "paper_id": candidate["paper_id"],
                    "candidate_repository_url": repository,
                    "association_verified": False,
                    "association_status": "PENDING_REVIEW",
                    # Use CURRENT paper-link evidence, not old evidence.
                    "paper_link_evidence": candidate["evidence"],
                    "github": cache[repository],
                }
            )
        else:
            pending.append(
                {
                    **candidate,
                    "repository_url": repository,
                }
            )

    return cached_rows, pending


def merge_rows(
    cached: list[dict],
    fetched: list[dict],
    pending: list[dict],
) -> list[dict]:
    expected = {
        (
            candidate["paper_id"],
            candidate["repository_url"],
        ): candidate
        for candidate in pending
    }

    fetched_by_pair = {}

    for row in fetched:
        key = pair_key(row)

        if key in fetched_by_pair:
            raise ValueError("Duplicate fetched observation.")

        fetched_by_pair[key] = row

    if set(fetched_by_pair) != set(expected):
        raise ValueError(
            "Fetched observations do not exactly match "
            "the pending worklist."
        )

    merged = list(cached)

    for key, candidate in expected.items():
        row = fetched_by_pair[key]

        merged.append(
            {
                **row,
                "association_verified": False,
                "association_status": "PENDING_REVIEW",
                "paper_link_evidence": candidate["evidence"],
            }
        )

    keys = [pair_key(row) for row in merged]

    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate pair in merged observations.")

    return merged


def plan(
    candidates_path: Path,
    history_dir: Path,
    plan_dir: Path,
    max_age_hours: float,
) -> dict:
    if max_age_hours <= 0:
        raise ValueError("max-age-hours must be positive.")

    candidates = read_jsonl(candidates_path)

    cache = find_cached_metrics(
        history_dir,
        now=datetime.now(timezone.utc),
        max_age=timedelta(hours=max_age_hours),
    )

    cached, pending = build_worklist(candidates, cache)

    # Do not overwrite a plan that might already be in use.
    plan_dir.mkdir(parents=True, exist_ok=False)

    write_jsonl(
        plan_dir / "candidates.snapshot.jsonl",
        candidates,
    )

    write_jsonl(
        plan_dir / "cached_observations.jsonl",
        cached,
    )

    write_jsonl(
        plan_dir / "pending_candidates.jsonl",
        pending,
    )

    summary = {
        "total_candidate_pairs": len(candidates),
        "cached_candidate_pairs": len(cached),
        "pending_candidate_pairs": len(pending),
        "pending_unique_repositories": len(
            {row["repository_url"] for row in pending}
        ),
        "max_cache_age_hours": max_age_hours,
        "plan_dir": str(plan_dir),
    }

    (plan_dir / "plan.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    return summary


def merge(
    plan_dir: Path,
    fetched_path: Path | None,
) -> dict:
    cached = read_jsonl(
        plan_dir / "cached_observations.jsonl"
    )

    pending = read_jsonl(
        plan_dir / "pending_candidates.jsonl"
    )

    if pending and fetched_path is None:
        raise ValueError(
            "--fetched is required when pending candidates exist."
        )

    fetched = (
        read_jsonl(fetched_path)
        if fetched_path is not None
        else []
    )

    rows = merge_rows(cached, fetched, pending)

    output = plan_dir / "merged_observations.jsonl"
    write_jsonl(output, rows)

    return {
        "merged_candidate_associations": len(rows),
        "cached_associations": len(cached),
        "newly_fetched_associations": len(fetched),
        "output": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan and merge incremental GitHub enrichment."
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    plan_parser = commands.add_parser("plan")

    plan_parser.add_argument(
        "--candidates",
        type=Path,
        default=Path(
            "data/arxiv/repository_candidates.jsonl"
        ),
    )

    plan_parser.add_argument(
        "--history-dir",
        type=Path,
        default=Path("data/github"),
    )

    plan_parser.add_argument(
        "--plan-dir",
        required=True,
        type=Path,
    )

    plan_parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24,
    )

    merge_parser = commands.add_parser("merge")

    merge_parser.add_argument(
        "--plan-dir",
        required=True,
        type=Path,
    )

    merge_parser.add_argument(
        "--fetched",
        type=Path,
    )

    args = parser.parse_args()

    try:
        if args.command == "plan":
            result = plan(
                args.candidates,
                args.history_dir,
                args.plan_dir,
                args.max_age_hours,
            )
        else:
            result = merge(
                args.plan_dir,
                args.fetched,
            )

    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Worklist operation failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()