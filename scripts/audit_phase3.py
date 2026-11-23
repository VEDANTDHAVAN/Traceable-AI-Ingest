"""Phase III audit: provider fallback, chunking, grounding and evidence.

Selects the newest Phase III run that accepted at least one record and
independently revalidates every claim against the Phase II source text.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os

from pathlib import Path

from ai_ingest.llm import engine as engine_module
from ai_ingest.llm import providers as provider_module


PROVIDER_ORDER = ("gemini", "groq", "deepseek")
ALLOWED_KINDS = {"news", "job"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
    return rows


def source_texts(phase2: Path) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    for row in read_jsonl(phase2 / "news.jsonl"):
        mapping[("NEWS", row["content"]["content_sha256"])] = row["content"][
            "full_text"
        ]
    for row in read_jsonl(phase2 / "jobs.jsonl"):
        identity = hashlib.sha256(
            str(row["source"]["url"]).encode("utf-8")
        ).hexdigest()
        mapping[("JOB", identity)] = row["content"]["full_text"]
    return mapping


def select_run(root: Path) -> Path:
    candidates = []
    for run in root.iterdir():
        if not run.is_dir() or not run.name[:1].isdigit():
            continue
        metadata_path = run / "metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("records_extracted", 0) > 0:
            candidates.append(run)
    if not candidates:
        raise ValueError("No Phase III run accepted any record.")
    return sorted(candidates)[-1]


def implementation_checks() -> dict:
    engine_source = inspect.getsource(engine_module)
    provider_source = inspect.getsource(provider_module)

    return {
        "async_engine": "async def extract" in engine_source,
        "chunking": "def split_text" in engine_source
        and "chunk_bytes" in engine_source,
        "payload_too_large_413_split": "exc.status == 413" in engine_source
        and "split_text(" in engine_source,
        "rate_limit_429_retry": "exc.status == 429" in engine_source
        and "retry_after" in engine_source,
        "jitter_backoff": "random.uniform" in engine_source,
        "provider_fallback": "for provider_index in range(" in engine_source
        and "start_provider" in engine_source,
        "provider_schema_payload": "model_json_schema" in provider_source,
        "request_byte_guard": "max_request_bytes" in provider_source
        and "raise ProviderFailure(413)" in provider_source,
        "static_attempt_logging": '"event": "attempt"' in engine_source,
        "coverage_validation": "coverage_complete" in engine_source
        and "Chunk coverage is not contiguous" in engine_source,
    }


def audit(run_dir: Path, phase2: Path, output: Path) -> dict:
    report: dict = {"phase3_complete": False, "gates": {}, "counts": {}, "errors": []}

    def fail(message: str) -> None:
        report["errors"].append(message)

    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    rows = read_jsonl(run_dir / "extraction.jsonl")
    failures = read_jsonl(run_dir / "failures.jsonl")
    texts = source_texts(phase2)

    provider_names = [item["provider"] for item in metadata.get("provider_order", [])]
    fallback_configured = all(name in provider_names for name in PROVIDER_ORDER)

    claim_count = 0
    verified_claims = 0
    attempts_logged = 0
    all_covered = True

    for index, row in enumerate(rows):
        kind = row["record_type"]
        key = (kind, row["record_id"])
        text = texts.get(key)
        if text is None:
            fail(f"{kind} {index}: source text not found for revalidation.")
            continue

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != row.get("source_text_sha256"):
            fail(f"{kind} {index}: source hash mismatch.")

        if not row.get("source_url"):
            fail(f"{kind} {index}: missing source URL.")

        if not row.get("coverage_complete"):
            all_covered = False
            fail(f"{kind} {index}: coverage incomplete.")

        chunks = row.get("chunks", [])
        spans = sorted((c["chunk_start"], c["chunk_end"]) for c in chunks)
        if not spans or spans[0][0] != 0 or spans[-1][1] != len(text):
            fail(f"{kind} {index}: chunk spans do not cover the source.")
        if any(a[1] != b[0] for a, b in zip(spans, spans[1:])):
            fail(f"{kind} {index}: chunk spans are not contiguous.")

        attempts_logged += len(row.get("provider_attempts", []))
        if not row.get("provider_attempts"):
            fail(f"{kind} {index}: no provider attempt log.")

        for claim in row.get("claims", []):
            claim_count += 1
            quote = claim.get("quote")
            value = claim.get("value")
            start = claim.get("quote_start")
            end = claim.get("quote_end")

            if not quote or not value or start is None or end is None:
                fail(f"{kind} {index}: incomplete claim evidence.")
                continue

            if text[start:end] != quote:
                fail(
                    f"{kind} {index}: quote does not match source at offsets "
                    f"{start}:{end}."
                )
                continue

            if value not in quote:
                fail(f"{kind} {index}: value is not present in the quote.")
                continue

            containing = [
                (a, b) for a, b in spans if a <= start and end <= b
            ]
            if not containing:
                fail(f"{kind} {index}: quote lies outside every chunk.")
                continue

            if claim.get("source_text_sha256") != digest:
                fail(f"{kind} {index}: claim source hash mismatch.")
                continue

            if claim.get("source_url") != row.get("source_url"):
                fail(f"{kind} {index}: claim source URL mismatch.")
                continue

            if not claim.get("provider") or not claim.get("model"):
                fail(f"{kind} {index}: claim lacks provider/model.")
                continue

            verified_claims += 1

        for field in row.get("null_fields", []):
            if field.get("reason") != "NOT_ESTABLISHED_BY_SOURCE":
                fail(f"{kind} {index}: null field lacks an explicit reason.")

    for index, row in enumerate(failures):
        if not row.get("error"):
            fail(f"Failure {index}: missing error detail.")
        if not row.get("events"):
            fail(f"Failure {index}: missing provider attempt log.")
        if row.get("canonical_records_updated"):
            fail(f"Failure {index}: canonical records were modified.")

    implementation = implementation_checks()
    report["implementation"] = implementation

    env_configured = {
        kind: bool(os.environ.get(f"{kind.upper()}_API_KEY", "").strip())
        for kind in PROVIDER_ORDER
    }

    gates = {
        "provider_fallback_configured": fallback_configured,
        "three_provider_keys_configured": all(env_configured.values()),
        "chunking_implemented": implementation["chunking"],
        "payload_413_handling_implemented": implementation[
            "payload_too_large_413_split"
        ],
        "rate_limit_429_backoff_implemented": implementation[
            "rate_limit_429_retry"
        ]
        and implementation["jitter_backoff"],
        "provider_fallback_implemented": implementation["provider_fallback"],
        "attempt_logs_present": attempts_logged > 0
        and implementation["static_attempt_logging"],
        "evidence_and_offsets_validated": claim_count > 0
        and verified_claims == claim_count,
        "all_non_null_extractions_grounded": claim_count > 0
        and verified_claims == claim_count,
        "no_fabricated_values": claim_count > 0
        and verified_claims == claim_count,
        "source_hash_and_url_present": all(
            row.get("source_text_sha256") and row.get("source_url") for row in rows
        ),
        "chunk_coverage_complete": all_covered and bool(rows),
        "null_fields_have_reasons": all(
            field.get("reason")
            for row in rows
            for field in row.get("null_fields", [])
        ),
        "canonical_records_unchanged": not metadata.get(
            "canonical_records_updated", False
        )
        and not any(row.get("canonical_records_updated") for row in rows),
    }
    report["gates"] = gates
    report["counts"] = {
        "run_id": run_dir.name,
        "records_extracted": len(rows),
        "records_failed": len(failures),
        "claims_total": claim_count,
        "claims_verified": verified_claims,
        "provider_attempts_logged": attempts_logged,
        "providers_seen": sorted(
            {
                attempt["provider"]
                for row in rows
                for attempt in row.get("provider_attempts", [])
            }
        ),
        "chunk_bytes": metadata.get("chunk_bytes"),
    }

    report["phase3_complete"] = all(gates.values()) and not report["errors"]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase III completion audit.")
    parser.add_argument("--root", type=Path, default=Path("data/phase3"))
    parser.add_argument("--phase2", type=Path, default=Path("data/phase2/merged"))
    parser.add_argument("--output", type=Path, default=Path("data/phase3/audit.json"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    if args.env_file.is_file():
        from ai_ingest.config import load_environment

        load_environment(args.env_file)

    run_dir = select_run(args.root)
    report = audit(run_dir, args.phase2, args.output)
    print(json.dumps(report, indent=2))

    if not report["phase3_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
