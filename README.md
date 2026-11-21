# Traceable AI Ingest

Async, source-traceable ingestion pipeline for startups, products, AI research papers, jobs, and news. The project is structured to start locally and grow to distributed workers.

## Principles
- Every record retains its source URL and source name.
- LLMs may extract fields from supplied source text; they must not invent values.
- Missing or unverifiable values remain null and are flagged.
- Freshness is based on normalized publication time, not collection time.
- Respect source terms, published API limits, and access controls. Do not bypass CAPTCHAs or anti-bot protections; prefer official APIs, feeds, partnerships, or permitted browser rendering.

## Quick start
1. Create and activate a Python 3.11+ virtual environment.
2. Install: `pip install -e '.[dev]'`
3. Copy `.env.example` to `.env` and configure only the providers you plan to use.
4. Run tests: `pytest`
5. Run CLI help: `ai-ingest --help`

No real ingestion is enabled in this skeleton. Source adapters, credentials, and per-source compliance settings must be added before running crawls.

## Layout
- `src/ai_ingest/`: application, schemas, source adapters, storage, extraction, resolution.
- `configs/`: source registry and policy configuration.
- `tests/`: unit and integration tests.
- `docs/`: architecture, source inventory, decisions, and quality gates.
- `data/`: local-only outputs (gitignored).

## Record provenance
Records should include `source.name`, `source.url`, `collectedAt`, and (where relevant) publication time and evidence. See `docs/data-contract.md`.
