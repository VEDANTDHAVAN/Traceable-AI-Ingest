# Access, robots and anti-bot compliance

This project collects public, structured data for research. It does **not**
attempt to defeat access controls, and it makes no bypass capability claim.

## Operating rules

1. Prefer documented APIs, RSS/Atom feeds, sitemaps and published data exports.
2. Check `robots.txt` before every HTML article fetch. A fetch is skipped when
   the fetched policy disallows it; the item falls back to the feed summary and
   the extraction status is recorded as `FEED_SUMMARY`.
3. Apply per-domain concurrency caps and minimum spacing. The default is two
   concurrent requests per domain with 0.4–0.6 s spacing.
4. Retry only transient failures (429, 5xx, timeouts) with exponential backoff
   and jitter, bounded by a retry budget and a per-source circuit breaker.
5. Respect `Retry-After`.
6. Never rotate proxies or spoof browser fingerprints to evade a block.
7. Never automate a human-verification challenge.
8. Log every blocked request, rate-limit event and disallowed robots path in
   the run's `http_report.json`.

## Implemented controls

| Control | Implementation |
|---|---|
| Async I/O | `asyncio` + `aiohttp` throughout (`src/ai_ingest/net.py`, `news_jobs.py`) |
| Bounded concurrency | `asyncio.Semaphore` per source policy |
| Per-domain pacing | Lock + monotonic clock spacing per domain |
| robots.txt | `urllib.robotparser.RobotFileParser`, cached per origin |
| Backoff | Exponential with uniform jitter, `Retry-After` honoured |
| Retry budget | `SourcePolicy.retry_budget` (default 2–3) |
| Circuit breaker | Opens after five consecutive failures, half-opens after 60 s |
| Telemetry | Per-source request/retry/rate-limit/blocked counters and event log |
| Bypass capability | None implemented; no stealth/proxy/challenge-solving code |
| Browser rendering | Not required for any selected source; Playwright is not used |

## Selected sources and their access basis

### News (RSS/Atom feeds published for syndication)

| Source | Endpoint | Basis |
|---|---|---|
| TechCrunch AI | `techcrunch.com/category/artificial-intelligence/feed/` | Publisher RSS |
| The Verge AI | `theverge.com/rss/ai-artificial-intelligence/index.xml` | Publisher RSS |
| MIT News AI | `news.mit.edu/rss/topic/artificial-intelligence2` | Publisher RSS |
| MarkTechPost | `marktechpost.com/feed/` | Publisher RSS |
| Analytics India Magazine | `analyticsindiamag.com/feed/` | Publisher RSS |
| SiliconANGLE | `siliconangle.com/feed/` | Publisher RSS |

Article text is fetched only when robots.txt allows it. When it does not, the
record retains the feed summary and says so.

### Jobs (documented JSON APIs and official ATS boards)

| Board | Endpoint | Basis |
|---|---|---|
| OpenAI Careers | `api.ashbyhq.com/posting-api/job-board/openai` | Official Ashby board API |
| Cohere Careers | `api.ashbyhq.com/posting-api/job-board/cohere` | Official Ashby board API |
| Anthropic Careers | `boards-api.greenhouse.io/v1/boards/anthropic/jobs` | Official Greenhouse board API |
| Jobicy | `jobicy.com/api/v2/remote-jobs` | Public JSON API |
| Arbeitnow | `arbeitnow.com/api/job-board-api` | Public JSON API |
| Himalayas | `himalayas.app/jobs/api` | Public JSON API |
| Remote OK | `remoteok.com/api` | Public API with attribution requirement |
| Remotive | `remotive.com/api/remote-jobs` | Public JSON API |

Remote OK's published API terms require a follow link and source attribution
when its data is displayed. Any export includes the `Remote OK` source name and
the original job URL. Remotive timestamps are timezone-naive, so they are
quarantined as `UNKNOWN_PUBLICATION_DATE` rather than assumed to be UTC.

### Datasets and research

| Source | Basis |
|---|---|
| AI Market Watch startup dataset | CC BY 4.0, attribution retained |
| AutoVenture AI tools dataset | CC BY 4.0 |
| Awesome AI Tools | CC0 1.0 |
| AIToolBuzz AI tools database | CC BY-SA 4.0 |
| Papers with Code archive links | CC BY-SA 4.0 |
| arXiv metadata | arXiv API, documented request spacing |
| GitHub repository metrics | GitHub REST API, token-aware rate limits |

## Protected or restricted sources: compliant strategy

These were evaluated and are deliberately **not** scraped with automation.

| Source | Observed restriction | Strategy |
|---|---|---|
| VentureBeat AI feed | HTTP 429 during evaluation | Pause and omit; subscribe to the feed or use a licensed content API instead of retrying aggressively |
| Artificial Intelligence News | HTTP 403 | Contact the publisher for an API or syndication agreement |
| Datanami | HTTP 403 | Use the publisher's newsletter/feed or request a data licence |
| Unite.AI | HTTP 403 | Manual review or partnership only |
| Cloudflare/Datadome-protected directories | Challenge pages | Do not automate; use the operator's public API or a data-export agreement |
| LinkedIn, Indeed, Glassdoor | Authentication and Terms of Service restrictions | Official partner APIs or licensed feeds only |
| X/Twitter | Authenticated API tiers | Official API within plan limits |
| Product Hunt | Authenticated GraphQL API | Official API token and rate limits |
| Gartner, Forrester, paywalled analysts | Paywall | Purchase/licence; never bypass |
| Individual startup profile pages | Terms vary, many challenge-protected | Secondary dataset retained; no automated verification claim |

When a source cannot be accessed compliantly, the pipeline records the blocked
status and the quarantine reason instead of substituting inferred data.

## Logging evidence

Each Phase II run writes `http_report.json` containing:

- the user agent string;
- every source policy (domain, concurrency cap, spacing, retry budget,
  robots mode);
- per-source requests, successes, failures, retries, rate-limit events,
  robots blocks, circuit-open events and bytes received;
- a chronological event log including `robots_disallowed`, `rate_limited`,
  `circuit_open` and per-request status and latency.

Blocked and rate-limited requests are therefore auditable rather than hidden.
