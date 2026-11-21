# Schema decisions

## Purpose

These models validate staged ingestion records.

Passing schema validation does not prove that a record is accurate,
fresh, unique, or eligible for final export. Those checks belong to
the evidence-validation and export-quality stages.

## Assessment field names

Preserve the assessment's field names, including:

- schemaVersion
- recordType
- source.name
- source.url
- collectedAt
- content.entityName
- content.data.employeeCount
- content.startupName
- content.pricingModel
- content.paper_url
- content.github_url
- content.github_stars
- content.published_date
- content.company
- content.date
- content.is_remote
- content.role_family

## Project extensions

Apply shared source provenance and collection timestamps to all
record types.

Add content.productName to distinguish a product from its owning
company.

Add content.github_metrics_collected_at to identify when a dynamic
star count was observed.

Add title and full_text to jobs.

Define a NEWS record containing title, date, and full_text because
the assessment requests news output without specifying its complete
schema.

## Missing information

Represent unknown values as null, never as fabricated facts.

Examples:

- Unknown employee count is null, not zero.
- Unknown pricing is null, not FREE.
- Unknown remote eligibility is null, not false.
- Unknown publication time is null, not collection time.
- Unavailable GitHub stars are null, not zero.

The final export gate must enforce required-field completeness.
Nullable staging fields do not relax submission requirements.

## Timestamp policy

Require timezone-aware timestamps.

Normalize accepted timestamps to UTC.

Keep publication time, collection time, and metrics-observation
time separate.

Do not interpret first-seen time as proof of publication time.

## Count policy

Employee counts and GitHub stars must be nonnegative integers
when known.

Reject booleans, numeric strings, negative values, and fractional
values.

## GitHub metrics

A known star count requires both a repository URL and a
metrics-observation timestamp.

A repository URL alone does not prove that the repository
implements the paper. The enrichment stage must preserve evidence
of the paper-to-repository relationship.

Structural validation does not verify that a URL exists or that
the supplied metrics are genuine.

## News and job freshness

Records with unknown or unverifiable publication times may enter
staging but must not enter the verified 24-hour output.

A later freshness gate will evaluate the publication evidence
against an explicit run timestamp.

## Unknown fields

Reject unexpected fields rather than silently ignoring them.

Schema changes should be intentional, documented, and tested.

## Next increment

Implement the arXiv source adapter with:

- Serialized, rate-limited requests.
- Raw-response persistence.
- Atom feed parsing.
- Pagination and checkpoints.
- Bounded retries.
- Offline parser and failure-path tests.

No live ingestion is implemented by this schema increment.