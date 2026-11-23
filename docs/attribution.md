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