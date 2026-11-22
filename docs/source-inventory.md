# Source inventory

To be completed before enabling live runs. For each source record: name, canonical domain, official API/feed, permitted use/terms, robots/access notes, rate limits, stable ID, publication-date fields, pagination/bulk options, and expected coverage.

## Currently enabled sources

| Source | Type | Access | Status |
|---|---|---|---|
| arXiv | RESEARCH_PAPER | Official Atom API (`export.arxiv.org/api/query`) | IMPLEMENTED |
| GitHub | RESEARCH_PAPER enrichment | Official REST API (`api.github.com/repos/{owner}/{repo}`), token via `GITHUB_TOKEN` | IMPLEMENTED |

## Planned sources (assessment Phase II)

5 AI news sources and 5 AI job boards are still to be selected and recorded
here with terms, rate limits, and publication-date fields before enabling.
See `docs/capability-matrix.md`.
