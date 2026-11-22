# Capability matrix — assessment gap analysis

Status of each assessment requirement against the repository as it stands.
States: **IMPLEMENTED**, **PARTIAL**, **MISSING**.

Audited against `src/ai_ingest/`, `configs/`, `tests/`, and `data/arxiv/papers.sqlite3`
(175 papers stored, 61 paper↔repository candidate pairs: 9 cached, 52 pending).
Test suite at audit time: 100 passing.

## A. Canonical output schemas (Pydantic v2)

| Requirement | State | Location / gap |
|---|---|---|
| STARTUP (`entityName`, `data.employeeCount`) | IMPLEMENTED | `src/ai_ingest/models.py:70-81` |
| PRODUCT (`startupName`, `pricingModel`) | PARTIAL | `models.py:84-92` — `productName` required, `startupName` optional; assessment wants canonical `startupName` |
| RESEARCH_PAPER (title, authors, paper_url, github_url/stars, published_date) | IMPLEMENTED | `models.py:95-123` (stricter: stars require url + observation timestamp) |
| JOB (company, date, is_remote, role_family) | PARTIAL | `models.py:126-137` — adds required `title`/`full_text`; `company` optional |
| NEWS (headline, summary, full_text, published_date, entities_mentioned) | PARTIAL | `models.py:140-148` — has `title`/`date`/`full_text`; missing `headline`, `summary`, `published_date`, `entities_mentioned` |
| Entity Mapping Log (raw_name, canonical_name, match_method, confidence_score, source_url) | MISSING | no model; planned in `docs/schema-decisions.md` |

## B. Pipeline capabilities

| Phase | Requirement | State | Evidence / gap |
|---|---|---|---|
| I | Bulk research papers (arXiv) | IMPLEMENTED | `sources/arxiv.py:247`, `arxiv_bulk.py:54`, `storage/sqlite.py:54` |
| I | GitHub `stargazers_count` with `GITHUB_TOKEN` + rate-limit tracking | IMPLEMENTED | `github_metrics.py:290`, `repository_links.py:120`, `verify_associations.py:50` |
| I | ≥1,000 startups / products / papers | PARTIAL | 175 papers stored; no startup/product adapters; no Papers-with-Code |
| I | 500k+ scale via worker/queue config | MISSING | `TCPConnector(limit=1)` (`github_metrics.py:325`), SQLite single-writer |
| II | 5 AI news + 5 AI job sources | MISSING | `configs/sources.yaml` is `sources: []` |
| II | Full-text article/job extraction | MISSING | no adapters or parsers |
| II | Date normalization (ISO/RFC/JSON-LD/relative) | MISSING | `python-dateutil` declared, unused |
| II | Freshness ledger (URL + content hash) | MISSING | — |
| II | 24h freshness gate for JOB/NEWS | MISSING | contract only (`docs/data-contract.md:13-14`) |
| III | LLM chain Gemini→Groq→DeepSeek | MISSING | env keys only (`.env.example:5-7`) |
| III | HTML pruning + token-aware chunking (no 413) | MISSING | — |
| III | Per-provider semaphores + backoff/jitter + failover | MISSING | — |
| IV | 50+ canonical seed registry | MISSING | `configs/` has only empty `sources.yaml` |
| IV | Exact→normalized→fuzzy matcher + mapping log | MISSING | — |
| V | 6-tab `.xlsx` / Sheets exporter | MISSING | — |
| VI | README setup/run commands | PARTIAL | `README.md` still says no ingestion enabled (stale) |
| VI | Root `architecture.pdf` (≤3 pages) | MISSING | — |

## C. Schema / contract discrepancies

- `docs/schema-decisions.md:11-48` documents assessment field names vs. project
  extensions (`productName`, `github_metrics_collected_at`, job `title`/`full_text`,
  NEWS as `title`/`date`/`full_text`).
- `docs/data-contract.md:11` states the record-type minimum contract.
- PRODUCT/JOB/NEWS drift from the assessment field names is intentional staging
  but must be reconciled before export.
- Entity Mapping Log is absent from both schema and docs.

## D. Declared but unused dependencies (intended extension points)

`pydantic-settings`, `structlog`, `feedparser`, `beautifulsoup4`, `tenacity`,
`python-dateutil` are in `pyproject.toml` but imported nowhere in `src/`.
