import importlib.util
import json
import re

from pathlib import Path


def load_script(name: str):
    path = Path("scripts") / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_phase2_merge_and_audit_passes_on_valid_run(tmp_path: Path):
    audit_phase2 = load_script("audit_phase2.py")
    root = tmp_path / "phase2"
    run = root / "20260101T120000000000Z"
    run.mkdir(parents=True)

    per_source = {
        f"news:{key}": {
            "name": key,
            "queried": True,
            "entries": 1,
            "exported": 0,
            "duplicates": 0,
            "quarantined": 0,
        }
        for key in ("a", "b", "c", "d", "e", "f")
    }
    per_source.update(
        {
            f"jobs:{key}": {
                "name": key,
                "queried": True,
                "entries": 1,
                "exported": 0,
                "duplicates": 0,
                "quarantined": 0,
            }
            for key in ("a", "b", "c", "d", "e")
        }
    )

    (run / "metadata.json").write_text(
        json.dumps(
            {
                "per_source": per_source,
                "max_concurrent_requests": 2,
                "retry_count": 0,
                "rate_limit_events": 0,
                "wall_clock_seconds": 1.0,
                "user_agent": "fixture-agent",
            }
        ),
        encoding="utf-8",
    )

    write_jsonl(
        run / "news.jsonl",
        [
            {
                "schemaVersion": "1.0",
                "recordType": "NEWS",
                "source": {"name": "Fixture News", "url": "https://example.com/a"},
                "collectedAt": "2026-01-01T12:00:00Z",
                "content": {
                    "title": "Fixture article",
                    "date": "2026-01-01T11:00:00Z",
                    "full_text": "Fixture article text.",
                    "canonical_url": "https://example.com/a",
                    "content_sha256": "a" * 64,
                    "freshness_status": "FRESH_24H",
                    "extraction_status": "FULL_TEXT",
                    "ingested_at": "2026-01-01T12:00:00Z",
                },
            }
        ],
    )
    write_jsonl(
        run / "jobs.jsonl",
        [
            {
                "schemaVersion": "1.0",
                "recordType": "JOB",
                "source": {"name": "Fixture Jobs", "url": "https://example.com/job"},
                "collectedAt": "2026-01-01T12:00:00Z",
                "content": {
                    "title": "AI Engineer",
                    "company": None,
                    "date": "2026-01-01T10:00:00Z",
                    "isRemote": True,
                    "roleFamily": "Engineering",
                    "full_text": "Build machine learning systems.",
                    "extraction_status": "FEED_SUMMARY",
                },
            }
        ],
    )
    write_jsonl(
        run / "provenance.jsonl",
        [
            {
                "record_type": "NEWS",
                "canonical_url": "https://example.com/a",
                "source_backed": True,
                "independently_verified": False,
                "raw_sha256": "b" * 64,
                "attribution": "Fixture",
            },
            {
                "record_type": "JOB",
                "job_url": "https://example.com/job",
                "source_backed": True,
                "raw_sha256": "c" * 64,
                "ai_match_terms": ["ai"],
                "company_canonical": None,
                "company_resolution_status": "UNRESOLVED",
                "ownership_verification_status": "NAME_MATCH_ONLY",
            },
        ],
    )
    write_jsonl(run / "duplicates.jsonl", [])
    write_jsonl(
        run / "quarantine.jsonl",
        [
            {
                "kind": "NEWS",
                "source": "a",
                "reason": "UNKNOWN_PUBLICATION_DATE",
                "link": "https://example.com/undated",
            }
        ],
    )
    (root / "registry.sqlite3").write_bytes(b"")

    merged = root / "merged"
    audit_phase2.merge_runs(root, merged)
    report = audit_phase2.audit(merged, tmp_path / "audit.json")

    assert report["phase2_complete"] is True
    assert report["counts"]["news_sources_queried"] == 6
    assert report["counts"]["jobs_exported"] == 1


def test_architecture_pdf_is_at_most_three_pages(tmp_path: Path):
    module = load_script("make_architecture_pdf.py")
    lines = module.wrap_blocks(module.BLOCKS)
    pages = module.paginate(lines, per_page=60)
    assert len(pages) <= 3

    output = tmp_path / "architecture.pdf"
    module.build_pdf(pages, output)
    payload = output.read_bytes()
    assert len(re.findall(rb"/Type /Page\b", payload)) == len(pages)
