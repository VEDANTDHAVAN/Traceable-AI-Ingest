from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, HttpUrl

RecordType = Literal["STARTUP", "PRODUCT", "RESEARCH_PAPER", "JOB", "NEWS"]

class SourceRef(BaseModel):
    name: str
    url: HttpUrl

class BaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: str = "1.0"
    recordType: RecordType
    source: SourceRef
    collectedAt: datetime
