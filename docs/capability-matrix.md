# Capability matrix

Status of each assessment requirement. States: **EXECUTED**, **IMPLEMENTED**
(code + tests, not necessarily executed at production scale), **BLOCKED**.

| Requirement | State | Evidence |
|---|---|---|
| Phase I: ≥1,000 unique startups | EXECUTED | 4,656; `data/phase1/audit.json` |
| Phase I: ≥1,000 unique products | EXECUTED | 24,938 merged from 3 licensed sources |
| Phase I: ≥1,000 unique papers | EXECUTED | 1,790 papers |
| Phase I: ≥1,000 paper→repo associations | EXECUTED | 1,660 accepted associations |
| Phase I: current GitHub stars | EXECUTED | live GitHub API, observation timestamp stored |
| Phase I: source hash + provenance verification | EXECUTED | audit re-hashes raw snapshots |
| Phase I: bounded concurrency / resumability | EXECUTED | Papers with Code: 4 arXiv + 8 GitHub concurrency, page cache resume |
| Phase I: 500k scale design | IMPLEMENTED (design) | `docs/scale.md`; no load test |
| Phase II: 5+ news sources | EXECUTED | 6 feeds queried |
| Phase II: 5+ job boards | EXECUTED | 8 boards queried |
| Phase II: 24-hour freshness gate | EXECUTED | 18 news + 124 jobs; stale/undated quarantined |
| Phase II: full text where permitted | EXECUTED | `FULL_TEXT` / `FEED_SUMMARY` recorded per item |
| Phase II: distributed dedup registry | IMPLEMENTED | SQLite registry with atomic claims |
| Phase III: Gemini→Groq→DeepSeek chain | EXECUTED | live Gemini 503/429 → Groq accepted |
| Phase III: chunking, 413 split, 429 backoff | EXECUTED | attempt logs + unit tests |
| Phase III: grounded claims with offsets | EXECUTED | 8/8 claims revalidated; 1 ungrounded response rejected |
| Phase IV: ≥50 seed entities | EXECUTED | 66 entities / 164 names |
| Phase IV: deterministic resolution | EXECUTED | exact/alias only; 4,782 mapping rows |
| Phase IV: no ownership inference | EXECUTED | `ownership_verified=false` everywhere |
| Phase V: async, bounded, robots, backoff, circuit breaker | EXECUTED | `src/ai_ingest/net.py`; blocked events logged |
| Phase V: no bypass claims/code | EXECUTED | `docs/compliance.md`; audit scans imports |
| Phase VI: architecture PDF ≤3 pages | EXECUTED | `architecture.pdf`, 2 pages |
| Six-tab export validated | EXECUTED | `data/exports/*.csv` + `metadata.json` |
| Public Google Sheet | BLOCKED | no Google credentials/service account available |

## Known limitations

- Startup employee counts are null: the source does not provide them.
- Company ownership is not resolved for products (0 resolved): no source
  establishes it. This is deliberate, not a failure to infer.
- AIToolBuzz has no pricing field, so its products retain null pricing.
- Product acquisition runs predate the shared HTTP telemetry; their per-source
  duration/retry counters are not instrumented. Paper and Phase II runs are.
- Papers without an accepted repository association have no stars (null).
- Groq Llama 3 was unavailable on the configured account (HTTP 404); the Groq
  provider was pointed at an available Groq model for the executed run. The
  provider order is unchanged. DeepSeek returned HTTP 402 (insufficient
  balance) and was not exercised live.
- The Google Sheet gate remains false until credentials are supplied; the six
  validated CSVs and uploader are ready.
