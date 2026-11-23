"""Generate architecture.pdf (max 3 pages) without external dependencies."""

from __future__ import annotations

import argparse
import textwrap

from pathlib import Path


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN = 42
FONT_SIZE = 9.2
LEADING = 11.6
WRAP = 104


def _ascii(value: str) -> str:
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u2192": "->",
        "\u2265": ">=",
        "\u2264": "<=",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value.encode("ascii", "replace").decode("ascii")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap_blocks(blocks: list[tuple[str, str]], width: int = WRAP) -> list[str]:
    lines: list[str] = []
    for kind, text in blocks:
        if kind == "h1":
            lines.append("")
            lines.append(text.upper())
        elif kind == "h2":
            lines.append("")
            lines.append(text)
        elif kind == "bullet":
            wrapped = textwrap.wrap(text, width=width - 3)
            lines.append(f"- {wrapped[0]}")
            lines.extend(f"  {part}" for part in wrapped[1:])
        else:
            lines.extend(textwrap.wrap(text, width=width))
    return lines


def paginate(lines: list[str], per_page: int) -> list[list[str]]:
    pages = []
    current: list[str] = []
    for line in lines:
        if line == "\f":
            pages.append(current)
            current = []
            continue
        current.append(line)
        if len(current) >= per_page:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    return pages


def build_pdf(pages: list[list[str]], output: Path) -> None:
    objects: list[bytes] = []

    def add_object(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add_object(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add_object(b"")  # placeholder, replaced below
    font_id = add_object(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )

    page_ids: list[int] = []
    content_ids: list[int] = []

    for lines in pages:
        commands = [
            "BT",
            f"/F1 {FONT_SIZE} Tf",
            f"{MARGIN} {PAGE_HEIGHT - MARGIN} Td",
            f"{LEADING} TL",
        ]
        for line in lines:
            commands.append(f"({_escape(_ascii(line))}) Tj T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", "replace")
        content_id = add_object(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        content_ids.append(content_id)
        page_id = add_object(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        page_ids.append(page_id)

    objects[pages_id - 1] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
        + " ".join(f"{page_id} 0 R" for page_id in page_ids)
        + "] >>"
    ).encode()

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    body = bytearray(header)
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_offset = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bytes(body))


BLOCKS: list[tuple[str, str]] = [
    ("h1", "Traceable AI Intelligence Ingestion - Architecture"),
    ("p", "Implemented reference pipeline: Phase I acquisitions, Phase II freshness ingestion, Phase III grounded LLM extraction, Phase IV deterministic entity resolution, Phase V compliance controls. Production scale components below are designed, not load-tested locally."),
    ("h2", "1. Scale to 500,000+ startups, products and papers"),
    ("bullet", "Partition every work stream by (source, shard hash of canonical URL or arXiv id). Each partition is an ordered, replayable task stream; no code change is needed to add shards."),
    ("bullet", "Durable queue (Kafka, SQS or Cloud Tasks) with visibility timeouts. Messages carry an idempotency key = sha256(record_type + canonical_url). Workers are stateless and horizontally scaled."),
    ("bullet", "Per-source distributed token buckets live in Redis; aggregate request rate stays within each publisher's published limit regardless of worker count. Per-domain semaphores become leased permits."),
    ("bullet", "Checkpoints are committed per page/offset, as the arXiv PaperStore already does. At-least-once delivery is safe because deduplication is deterministic on URL plus content hash."),
    ("bullet", "Raw snapshots are written before parsing. Replay re-derives records from saved bytes and SHA-256 manifests, so an interrupted crawl resumes without re-fetching."),
    ("h2", "2. 413 payload-too-large and 429 rate limit at thousands of concurrent extractions"),
    ("bullet", "The implemented engine chunks by UTF-8 bytes at paragraph boundaries, keeps every character, and validates contiguous coverage. A provider 413 marks the chunk for halving and re-dispatch to the same provider; smaller parts retain absolute character offsets."),
    ("bullet", "A local request-size guard rejects an oversized body before it leaves the worker (HTTPProvider max_request_bytes). Shrink factors are recorded per attempt, so chunk size adapts per provider/model."),
    ("bullet", "429 handling uses Retry-After when present, otherwise exponential backoff with uniform jitter, bounded by attempts, a retry budget and a per-provider cooldown. Requests are serialized per provider lock."),
    ("bullet", "When the budget or cooldown is exceeded, the next provider in Gemini Flash -> Groq Llama 3 -> DeepSeek is tried. Grounding validation is identical for every provider; a fallback cannot relax it."),
    ("bullet", "At cluster scale, cooldowns live in Redis per provider/model, and a global token bucket prevents thousands of workers from retrying in phase. 413/429 events are emitted as metrics and traces."),
    ("h2", "3. Freshness and distributed deduplication"),
    ("bullet", "Publication time comes only from structured fields (RSS/Atom, JSON, meta tags, JSON-LD, Unix epochs). Naive timestamps are rejected rather than assumed UTC. Unknown or future dates are quarantined, never backdated."),
    ("bullet", "Freshness is evaluated at ingestion against the record's own collectedAt, so the 24-hour gate is auditable later. Exported records store both published and ingested timestamps."),
    ("bullet", "Three identities are claimed atomically: canonical URL, normalized content hash, and record-type scoped identity. In production these are unique indexes and Redis SETNX claims keyed the same way, so two workers cannot process the same article or job."),
    ("bullet", "The processed registry is already a SQLite table keyed by (identity_kind, identity, record_type); it maps directly to a partitioned Postgres table or DynamoDB key."),
    ("h2", "4. Storage strategy"),
    ("bullet", "Primary operational database: PostgreSQL. Records, provenance, quarantine, entity mappings and crawl checkpoints need transactions, uniqueness constraints and relational audits. Partition by month and record type."),
    ("bullet", "Raw object storage: S3/R2, partitioned source/date/run_id, with SHA-256 manifests and immutable snapshots. Cheap, replayable, and the audit can re-hash every source."),
    ("bullet", "Vector storage: Qdrant or pgvector for news/job/paper embeddings, sharded by record type. Used for retrieval and related-content search, never as the source of truth for claims."),
    ("bullet", "Graph storage: Neo4j or Postgres edge tables for entity, product and repository relationships. Ownership edges carry evidence and a verification status; NAME_MATCH_ONLY edges are never promoted silently."),
    ("bullet", "Cache: Redis for robots policies, ETags, GitHub star TTLs and in-flight dedupe keys. Caches are disposable; correctness never depends on them."),
    ("h2", "5. Entity resolution"),
    ("bullet", "A 66-entity seed (164 canonical names and aliases) is normalized deterministically: NFKC, casefold, punctuation to spaces, trailing legal suffix removal. Exact/alias lookup only; fuzzy similarity never merges entities."),
    ("bullet", "Conflicting aliases fail at load time. Duplicate entity IDs fail. Unknown names are preserved as UNRESOLVED with a null canonical ID, and every mapping row retains raw name, normalized key, method, source URL, source-text hash and evidence."),
    ("bullet", "Resolution proves a name match, not ownership. The mapping log records ownership_verified=false and NAME_MATCH_ONLY for every match. Product names and domains are never used to infer a company."),
    ("bullet", "At scale, the alias index is a Postgres table plus a read-through cache; batch resolvers process 500k names with the same deterministic function."),
    ("h2", "6. LLM fallback, chunking, grounding and audit"),
    ("bullet", "Provider chain: Gemini Flash, Groq, DeepSeek. Every attempt records provider, model, status, latency, chunk bytes, outcome, retry and available token usage; raw provider responses are saved without credentials."),
    ("bullet", "Each non-null claim must contain a verbatim quote with character offsets into the normalized source text. Validation proves the quote exists at those offsets, the value occurs inside the quote, and the quote sits inside one chunk."),
    ("bullet", "Chunk spans must be contiguous and cover the full source. Invalid or ungrounded output is rejected and never retried into a fabrication; the next provider may try with the same rules."),
    ("bullet", "Phase III audit independently re-hashes the source and revalidates offsets. In the live run, 8/8 accepted claims revalidated; one provider response was rejected as ungrounded."),
    ("h2", "7. Anti-bot compliance"),
    ("bullet", "Only documented APIs, RSS/Atom feeds, public datasets and robots-permitted HTML are used. robots.txt is checked and cached per origin before article fetches; disallowed pages fall back to feed summaries and are marked FEED_SUMMARY."),
    ("bullet", "Per-domain concurrency caps, minimum spacing, retry budgets, circuit breakers and Retry-After handling are implemented. Blocked, rate-limited and policy-restricted sources are logged in each run's http_report.json."),
    ("bullet", "Protected sources (VentureBeat, Artificial Intelligence News, Datanami, Cloudflare/Datadome directories, LinkedIn, Product Hunt) are documented for official API, partnership, data-export or manual-review access. No stealth, proxy rotation or challenge-solving capability exists or is claimed."),
    ("bullet", "Playwright is not required for the selected structured sources and is not used. Browser rendering would be introduced only for a permitted, genuinely JavaScript-rendered source."),
    ("h2", "8. Observability, alerting, failure recovery and quality gates"),
    ("bullet", "Metrics per source: request rate, success/error rate, p95 latency, retries, 429s, robots blocks, circuit opens, records/s, quarantine rate, freshness rejections and grounding failures."),
    ("bullet", "Alerts on circuit-open, sustained 429s, zero-fresh sources, schema-validation spikes, ungrounded-output spikes and any failed audit gate. Traces carry run_id and idempotency key."),
    ("bullet", "Failure recovery: at-least-once queue, idempotent writes, checkpoint resume, dead-letter queue with status and attempt history, and raw-snapshot replay. Quarantine is a first-class output, not an error swallow."),
    ("bullet", "Publication gates: Phase I minimum counts and provenance; Phase II source coverage and 24-hour freshness; Phase III grounding; Phase IV seed/determinism/ownership; Phase V compliance. A failed gate blocks export."),
    ("h2", "Implemented vs designed"),
    ("p", "Implemented and executed locally: dataset acquisitions (4,656 startups, 24,938 products, 1,790 papers, 1,660 accepted paper-repo associations with live stars), 6 news feeds and 8 job boards with 24-hour gating (18 news, 124 jobs), 3 live LLM extractions with 8 grounded claims and provider fallback, 66-entity resolution with 4,782 mapping rows, robots/retry/circuit-breaker controls, and these audits."),
    ("p", "Designed but not executed at scale: distributed queue/workers, Redis rate coordination, PostgreSQL/object/vector/graph partitioning, 500k-record load test, dead-letter automation and production alerting. This document makes no throughput claim for those components."),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate architecture.pdf.")
    parser.add_argument("--output", type=Path, default=Path("architecture.pdf"))
    args = parser.parse_args()

    lines = wrap_blocks(BLOCKS)
    pages = paginate(lines, per_page=60)
    if len(pages) > 3:
        raise SystemExit(f"Content requires {len(pages)} pages; maximum is 3.")

    build_pdf(pages, args.output)
    print(f"wrote {args.output} with {len(pages)} pages")


if __name__ == "__main__":
    main()
