# Architecture notes (living document)

1. Source registry -> asynchronous fetchers -> immutable raw evidence -> parsers/LLM extraction -> schema validation -> entity resolution -> idempotent storage -> exports.
2. Adapters are source-specific. Prefer official APIs, bulk data endpoints, RSS/Atom, and permitted feeds. Use conservative per-host concurrency and obey documented limits.
3. Use deterministic parsers before LLM extraction. LLMs only fill schema fields supported by the supplied evidence; validate outputs and retain evidence.
4. Retry transient failures with bounded exponential backoff and jitter; respect Retry-After/reset headers. Do not retry permanent errors indefinitely.
5. Deduplicate by stable upstream ID where available; otherwise canonical URL plus source identity. Use database uniqueness constraints and idempotent upserts for cross-worker safety.
6. Keep source snapshots, extracted records, entity aliases, and run metadata distinct.
7. Never defeat access controls. For protected sources use approved APIs, licensed feeds, partner access, or mark unavailable.

Scaling design, 413/429 behavior, storage choices, and detailed freshness guarantees will be completed after source selection and implementation decisions.
