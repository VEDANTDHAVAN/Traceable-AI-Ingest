# Data contract and validation rules

## Common requirements
- Preserve the original source URL and source name for every record.
- Store collection time in UTC ISO-8601. Keep publication time distinct from collection time.
- Do not turn unknown values into guesses; use null and attach a validation reason.
- Store raw source snapshot or a content hash plus fetch timestamp where retention policy allows.
- Track provenance for every extracted field (source page, JSON-LD, official API, or other evidence).

## Target record types
The assessment schemas are the minimum contract: STARTUP, PRODUCT, RESEARCH_PAPER, and JOB. NEWS is required as a sheet output and should use an analogous schema with title, URL, publication timestamp, and full-text content.

## Freshness gate
A NEWS/JOB record is eligible only when its publication timestamp is verified and is within the configured 24-hour window at run time. If publication time cannot be established, quarantine it rather than claiming it is fresh.
