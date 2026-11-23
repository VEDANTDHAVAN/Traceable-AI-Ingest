# Third-party dataset attribution

## AutoVenture AI Tools Dataset

Publisher: AutoVenture (autoventure.live)

Dataset repository:
https://github.com/autoventure-projects/ai-tools-dataset

Publisher:
https://autoventure.live

License stated by the publisher: CC BY 4.0.

Attribution:
AutoVenture (autoventure.live), https://autoventure.live.

Changes:
Source fields are normalized into the assessment product schema.
Original pricing labels and source URLs are retained in provenance.
Free trials are not converted into FREE or FREEMIUM.
Company ownership is not inferred.

Snapshots:
Each ingestion run retains the downloaded dataset, license text,
collection timestamp, and SHA-256 hashes.

Limitations:
This is a secondary dataset. Source assertions have not automatically
been independently verified against every vendor website.
Staged records are not automatically final-export eligible.

## AI Market Watch — AI Startup Dataset

Repository:
https://github.com/runsmlee/ai-startup-dataset

Publisher:
https://www.ai-market-watch.com

License stated in the repository README: CC BY 4.0.

Required attribution:
Data: AI Market Watch, AI Startup Dataset.

Changes:
CSV fields normalized into startup records.
Exact source-profile duplicates removed.
Ambiguous normalized-name collisions quarantined.
Unknown employee counts retained as null.

Evidence:
Original CSV, README permission notice, collection timestamp,
and SHA-256 hashes retained per snapshot.

Limitations:
Publisher classifications are secondary-source assertions.
Individual profile pages are not automatically fetched or validated.

## AIToolBuzz AI Tools Database

Dataset:
https://huggingface.co/datasets/aitoolbuzz/ai-tools-database-25k-aitoolbuzz

License stated on the dataset card: CC-BY-SA-4.0.

Attribution:
Data: AIToolBuzz AI Tools Database (CC-BY-SA-4.0).

Changes:
CSV rows normalized into product records. Product name and official website
retained. Pricing is null because the observed CSV schema has no pricing field.
Company ownership is not inferred.

Limitations:
Directory listings are secondary assertions. They do not establish product
quality, pricing or company ownership.

## Papers with Code archive

Dataset:
pwc-archive/links-between-paper-and-code (Hugging Face datasets-server)

License: CC-BY-SA-4.0.

Attribution:
Data: Papers with Code archive links-between-paper-and-code (CC-BY-SA-4.0).

Changes:
Association rows are classified as OFFICIAL or COMMUNITY from the dataset's
is_official flag. Paper metadata is fetched from the arXiv API. GitHub stars and
collection timestamps are fetched from the GitHub REST API.

Limitations:
Community links are curated implementations, not official code. A GitHub star
count is an observation at collection time, not a quality claim.

## News and job sources

News records retain the publisher name, feed URL, article URL, publication
timestamp and attribution note. Job records retain the board name, job URL and
access basis documented in `docs/compliance.md`. Remote OK's published API terms
require source attribution and a follow link; the export includes both the
source name and the original job URL.

No article body is republished beyond what the source feed or a
robots-permitted page provided for extraction. Full text is stored only in the
local, gitignored data directory.

## Awesome AI Tools

Repository:
https://github.com/aliammari1/awesome-ai-tools

License:
CC0 1.0.

Required attribution:
Data: Awesome AI Tools.

Changes:
Structured catalog records normalized into product records.
Official URLs normalized.
Unknown pricing retained as null.
Company ownership not inferred from catalog entries.

Evidence:
Repository tree, individual tool JSON files, LICENSE file,
collection timestamp, and SHA-256 hashes retained per snapshot.

Limitations:
Catalog entries are third-party directory listings.
They do not establish company ownership.