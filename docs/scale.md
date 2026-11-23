# Scaling to 500,000+ records without code changes

The local pipeline is a single-process reference implementation. Production
scale comes from replacing the run loop's local queue, storage and scheduler,
not from rewriting adapters or validators.

## Work queue and partitioning

- Partition work by `(source, shard_key)` where `shard_key` is a stable hash of
  the canonical URL or arXiv identifier. Each partition is an independent,
  ordered task stream.
- Use a durable queue (SQS/Kafka/Cloud Tasks) with visibility timeouts. Every
  message carries an idempotency key: `sha256(record_type + canonical_url)`.
- The existing `ProcessedRegistry` schema maps directly to a partitioned
  deduplication table keyed by `(identity_kind, identity, record_type)`.

## Distributed workers

- Stateless workers claim messages, perform bounded async fetches, validate and
  write results. Workers never rely on local memory for correctness.
- Per-source token buckets are coordinated in Redis (or the queue's native
  concurrency limits) so the aggregate request rate stays within published
  limits even with hundreds of workers.
- Each worker runs the same `AsyncFetcher` policies; semaphores become
  distributed leases keyed by domain.

## Idempotency and checkpointing

- At-least-once delivery is assumed. Deterministic deduplication on canonical
  URL + content hash makes duplicate delivery harmless.
- Checkpoints are stored per crawl/source (as `PaperStore` already does for
  arXiv pages). A resumed job continues from the last committed page/offset.
- Raw snapshots are written before parsing; a replay job can re-derive every
  derived row from saved bytes and hashes.

## Retry, dead letters and quarantine

- Retries use exponential backoff with jitter and a bounded budget per source.
- Exhausted messages go to a dead-letter queue with the status code, attempt
  count, URL and run ID.
- Records that fail schema, freshness, grounding or hash verification go to the
  quarantine stream with a machine-readable reason; they are never silently
  dropped or counted as successes.

## Storage partitioning

- Operational database: PostgreSQL with monthly partitions for `records`,
  `provenance`, `quarantine` and `entity_mapping`; unique indexes on canonical
  URL and content hash.
- Raw objects: object storage partitioned by `source/date=YYYY-MM-DD/run_id/`
  with SHA-256 manifests.
- Vector index: sharded collections per record type, one embedding per
  document/chunk, metadata filters for freshness and source.
- Graph store: entity and ownership edges only where evidence exists; a
  `NAME_MATCH_ONLY` edge is labelled as such and never promoted to ownership
  without independent evidence.
- Cache: Redis for robots policies, HTTP ETags, GitHub metrics (TTL), and
  in-flight deduplication keys.

## Monitoring and alerting

- Emit per-source request rate, error rate, p95 latency, retry rate,
  quarantine rate, freshness-gate rejection rate and coverage gaps.
- Alert on circuit-open events, sustained 429s, zero-freshness sources,
  schema-validation spikes and any run whose audit gates fail.
- Data-quality gates run as a separate job and block publication when a
  minimum, freshness window, provenance check or grounding check fails.

## What this local run proves

The local runs prove the adapters, validators, deduplication, freshness gate,
grounded extraction and audit rules execute correctly at small scale. They do
not prove 500,000-record throughput. That requires the distributed components
above and a load test against them.
