# Traceable AI Intelligence Ingestion

Evidence-first ingestion for AI startups, products, research papers, jobs and
news. Every exported record traces to a source URL, a preserved raw snapshot and
a SHA-256 hash. Unknown values stay `null`; unverifiable or stale items go to
quarantine instead of being inferred.

## Status at a glance

| Area | Verified result |
|---|---|
| Startups | 4,656 staged from 4,681 CSV rows (25 quarantined, 0 duplicates) |
| Products | 24,938 unique after merging 25,872 rows from 3 licensed sources |
| Research papers | 1,790 papers, 1,660 accepted paper-to-repository associations |
| GitHub metrics | live stars fetched for every accepted association |
| News | 6 feeds queried, 18 items inside the 24-hour gate |
| Jobs | 8 boards queried, 124 AI jobs inside the 24-hour gate |
| LLM extraction | 3 records, 8/8 non-null claims revalidated at exact offsets |
| Entity resolution | 66 seed entities, 4,782 mapping rows, 0 ownership claims |
| Tests | 200+ unit/integration tests |

Machine-readable audits live in `data/phase1/audit.json`,
`data/phase2/audit.json`, `data/phase3/audit.json`, `data/phase4/audit.json`,
`data/phase5/audit.json` and `data/final_audit.json`.

## Setup

1. Python 3.11+ and a virtual environment.
2. `pip install -e '.[dev]'`
3. Copy `.env.example` to `.env` and fill only the providers you will use.
   `GITHUB_TOKEN` raises GitHub API limits. Never commit `.env`.
4. `pytest`

## Architecture

- `src/ai_ingest/sources/` — dataset, feed and job-board adapters. Each adapter
  validates the observed schema and writes raw snapshots plus hashes.
- `src/ai_ingest/net.py` — bounded async HTTP: per-domain concurrency, spacing,
  robots.txt, Retry-After, exponential backoff with jitter, retry budgets and
  circuit breakers.
- `src/ai_ingest/freshness.py` — absolute/relative date normalization and the
  24-hour gate. Naive timestamps are rejected, never assumed UTC.
- `src/ai_ingest/content.py` — article text, metadata and canonical identities.
- `src/ai_ingest/registry.py` — processed-item registry for cross-run/node
  deduplication.
- `src/ai_ingest/llm/` — grounded multi-provider extraction with byte-bounded
  chunking, 413 splitting, 429 backoff, provider fallback and attempt logs.
- `src/ai_ingest/entity_resolution.py` — deterministic normalized exact/alias
  matching. No fuzzy merges; no ownership inference.
- `src/ai_ingest/merge_products.py` — cross-source product deduplication that
  retains every contributing source in provenance.
- `scripts/audit_phase*.py` — phase audits; `scripts/audit_final.py` — the
  end-to-end gate.
- `scripts/export_sheets.py` — validates and writes the six-tab export.
- `architecture.pdf` — 2-page production architecture (implemented vs designed).

## Commands

```bash
# Phase I
python -m ai_ingest.sources.ai_market_watch --help
python -m ai_ingest.sources.autoventure --help
python -m ai_ingest.sources.awesome_ai_tools
python -m ai_ingest.sources.aitoolbuzz
python -m ai_ingest.merge_products --autoventure <dir> --awesome <dir> --aitoolbuzz <dir> --output-dir <new-dir>
python src/ai_ingest/papers_with_code.py --pages 20 --cache-dir data/papers/pwc-cache
python scripts/audit_phase1.py --startups <snapshot> --products <merged> --papers <pwc-run>

# Phase II
python -m ai_ingest.news_jobs
python scripts/audit_phase2.py

# Phase III (billable calls require the explicit flag)
python -m ai_ingest.llm.news_jobs_extraction --allow-api-calls --limit 4
python scripts/audit_phase3.py

# Phase IV
python -m ai_ingest.entity_mapping --startups <snapshot>
python scripts/audit_phase4.py

# Phase V and final
python scripts/audit_phase5.py
python scripts/make_architecture_pdf.py
python scripts/export_sheets.py
python scripts/audit_final.py
```

## Six-tab export

`scripts/export_sheets.py` validates every row and writes six CSVs to
`data/exports/`. To publish them as a public Google Sheet:

1. Install the optional client: `pip install gspread google-auth`.
2. Create a Google service account with Sheets + Drive access and download its
   JSON key outside the repository.
3. Run:
   `python scripts/export_sheets.py --upload --credentials /path/to/key.json`
4. The uploader keeps exactly the six required tabs, writes the rows, shares the
   spreadsheet as anyone-with-link reader and records the URL in
   `data/exports/google_sheet.json`.

Do not commit the service-account key. The uploader never prints credentials.

## Evidence rules

- A record without a valid source URL is not exported.
- A product is not assigned to a company from its name, domain or description.
- A name match produces `NAME_MATCH_ONLY`; `ownership_verified` stays false.
- Non-null LLM claims require a verbatim quote and character offsets; the quote
  is revalidated against the source hash during audit.
- News and jobs outside the 24-hour window are quarantined with a reason.
- Anti-bot protections are respected. Protected sources are documented in
  `docs/compliance.md`; no bypass capability is implemented or claimed.

## Local data

`data/` is gitignored except `.gitkeep`: it contains raw snapshots, run outputs
and audit artifacts produced on this machine. Re-run the commands to rebuild it.

## Attribution

See `docs/attribution.md` for dataset licences, required attribution and the
limitations of secondary sources.
