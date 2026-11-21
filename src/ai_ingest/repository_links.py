import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET

from pathlib import Path
from urllib.parse import urlparse

from ai_ingest.sources.arxiv import ATOM, ArxivError, required_text
from ai_ingest.storage.sqlite import paper_identity


ARXIV = "{http://arxiv.org/schemas/atom}"

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    flags=re.IGNORECASE,
)

OWNER_PATTERN = re.compile(r"[A-Za-z0-9-]+")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")

# These are GitHub site routes, not repository owners for discovery.
RESERVED_OWNERS = {
    "about",
    "apps",
    "collections",
    "contact",
    "enterprise",
    "events",
    "explore",
    "features",
    "issues",
    "login",
    "marketplace",
    "new",
    "notifications",
    "organizations",
    "orgs",
    "pricing",
    "pulls",
    "search",
    "security",
    "settings",
    "signup",
    "sponsors",
    "topics",
    "trending",
    "users",
}


def canonical_repository_url(value: str) -> str | None:
    value = value.strip().rstrip(".,;:!?)]}")

    try:
        parsed = urlparse(value)

        if parsed.scheme.lower() not in {"http", "https"}:
            return None

        if parsed.hostname not in {"github.com", "www.github.com"}:
            return None

        if parsed.username or parsed.password:
            return None

        if parsed.port is not None:
            return None
    except ValueError:
        return None

    parts = parsed.path.strip("/").split("/")

    if len(parts) < 2:
        return None

    owner, repository = parts[:2]

    if owner.lower() in RESERVED_OWNERS:
        return None

    if repository.lower().endswith(".git"):
        repository = repository[:-4]

    if not OWNER_PATTERN.fullmatch(owner):
        return None

    if not REPOSITORY_PATTERN.fullmatch(repository):
        return None

    if repository in {".", ".."}:
        return None

    # Stable comparison form. Preserve the original URL in evidence.
    return f"https://github.com/{owner.lower()}/{repository.lower()}"


def entry_fields(entry: ET.Element):
    for label, tag in (
        ("summary", f"{ATOM}summary"),
        ("comment", f"{ARXIV}comment"),
    ):
        element = entry.find(tag)

        if element is not None:
            text = "".join(element.itertext())

            if text.strip():
                yield label, text

    for link in entry.findall(f"{ATOM}link"):
        href = link.attrib.get("href", "")

        if href:
            yield "link.href", href


def candidates_from_response(raw_path: Path) -> list[dict]:
    raw = raw_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ArxivError(
            f"Invalid XML in {raw_path}"
        ) from exc

    if root.tag != f"{ATOM}feed":
        raise ArxivError(f"Not an Atom feed: {raw_path}")

    candidates = []

    for entry in root.findall(f"{ATOM}entry"):
        source_url = required_text(entry, "id")
        paper_id = paper_identity(source_url)

        for field, text in entry_fields(entry):
            for match in URL_PATTERN.finditer(text):
                original_url = match.group(0)
                repository_url = canonical_repository_url(original_url)

                if repository_url is None:
                    continue

                snippet = text[
                    max(0, match.start() - 120):
                    min(len(text), match.end() + 120)
                ]

                candidates.append(
                    {
                        "paper_id": paper_id,
                        "paper_source_url": source_url,
                        "repository_url": repository_url,
                        "status": "CANDIDATE",
                        "association_verified": False,
                        "evidence": {
                            "kind": "arxiv_metadata_link",
                            "field": field,
                            "original_url": original_url,
                            "snippet": snippet,
                            "raw_path": str(raw_path.resolve()),
                            "raw_sha256": digest,
                        },
                    }
                )

    return candidates


def scan_responses(root: Path) -> tuple[list[dict], int]:
    if not root.is_dir():
        raise ValueError(f"Input directory does not exist: {root}")

    paths = sorted(root.rglob("response.xml"))

    if not paths:
        raise ValueError("No response.xml files found.")

    grouped = {}

    for raw_path in paths:
        for candidate in candidates_from_response(raw_path):
            key = (
                candidate["paper_id"],
                candidate["repository_url"],
            )

            if key not in grouped:
                grouped[key] = {
                    "paper_id": candidate["paper_id"],
                    "paper_source_url": candidate["paper_source_url"],
                    "repository_url": candidate["repository_url"],
                    "status": "CANDIDATE",
                    "association_verified": False,
                    "evidence": [],
                }

            evidence = candidate["evidence"]

            if evidence not in grouped[key]["evidence"]:
                grouped[key]["evidence"].append(evidence)

    return list(grouped.values()), len(paths)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover GitHub links in saved arXiv metadata."
    )

    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/arxiv"),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/arxiv/repository_candidates.jsonl"),
    )

    args = parser.parse_args()

    candidates, scanned_files = scan_responses(args.input_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    temporary = args.output.with_name(
        args.output.name + ".tmp"
    )

    with temporary.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(
                json.dumps(candidate, ensure_ascii=False) + "\n"
            )

    temporary.replace(args.output)

    print(
        json.dumps(
            {
                "raw_files_scanned": scanned_files,
                "unique_paper_repository_candidates": len(candidates),
                "papers_with_candidates": len(
                    {item["paper_id"] for item in candidates}
                ),
                "verified_associations": 0,
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()