from datetime import datetime, timezone
from ai_ingest.models import BaseRecord

def test_base_record_requires_provenance():
    record = BaseRecord(
        recordType="NEWS",
        source={"name": "Example", "url": "https://example.org/post"},
        collectedAt=datetime.now(timezone.utc),
    )
    assert str(record.source.url) == "https://example.org/post"
