from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    model_validator,
)


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


Timestamp = Annotated[
    AwareDatetime,
    AfterValidator(normalize_utc),
]

NonEmptyString = Annotated[
    str,
    Field(min_length=1),
]

NonNegativeCount = Annotated[
    int,
    Field(strict=True, ge=0),
]

RecordType = Literal[
    "STARTUP",
    "PRODUCT",
    "RESEARCH_PAPER",
    "JOB",
    "NEWS",
]

PricingModel = Literal[
    "FREE",
    "FREEMIUM",
    "PAID",
    "ENTERPRISE",
]


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class SourceRef(Model):
    name: NonEmptyString
    url: HttpUrl


class BaseRecord(Model):
    schemaVersion: Literal["1.0"] = "1.0"
    recordType: RecordType
    source: SourceRef
    collectedAt: Timestamp


class StartupData(Model):
    employeeCount: NonNegativeCount | None = None


class StartupContent(Model):
    entityName: NonEmptyString
    data: StartupData = Field(default_factory=StartupData)


class StartupRecord(BaseRecord):
    recordType: Literal["STARTUP"] = "STARTUP"
    content: StartupContent


class ProductContent(Model):
    productName: NonEmptyString
    startupName: NonEmptyString | None = None
    pricingModel: PricingModel | None = None


class ProductRecord(BaseRecord):
    recordType: Literal["PRODUCT"] = "PRODUCT"
    content: ProductContent


class ResearchPaperContent(Model):
    title: NonEmptyString
    authors: list[NonEmptyString] = Field(min_length=1)
    paper_url: HttpUrl
    github_url: HttpUrl | None = None
    github_stars: NonNegativeCount | None = None
    published_date: Timestamp
    github_metrics_collected_at: Timestamp | None = None

    @model_validator(mode="after")
    def validate_github_metrics(self) -> "ResearchPaperContent":
        if self.github_stars is not None:
            if self.github_url is None:
                raise ValueError(
                    "github_stars requires github_url."
                )

            if self.github_metrics_collected_at is None:
                raise ValueError(
                    "github_stars requires "
                    "github_metrics_collected_at."
                )

        return self


class ResearchPaperRecord(BaseRecord):
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    content: ResearchPaperContent


class JobContent(Model):
    title: NonEmptyString
    company: NonEmptyString | None = None
    date: Timestamp
    isRemote: StrictBool | None = None
    roleFamily: NonEmptyString | None = None
    full_text: NonEmptyString
    extraction_status: Literal["FULL_TEXT", "FEED_SUMMARY", "FAILED"] = (
        "FEED_SUMMARY"
    )


class JobRecord(BaseRecord):
    recordType: Literal["JOB"] = "JOB"
    content: JobContent


class NewsContent(Model):
    title: NonEmptyString
    date: Timestamp
    full_text: NonEmptyString
    canonical_url: HttpUrl
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    freshness_status: Literal["FRESH_24H"] = "FRESH_24H"
    extraction_status: Literal["FULL_TEXT", "FEED_SUMMARY", "FAILED"] = (
        "FEED_SUMMARY"
    )
    ingested_at: Timestamp


class NewsRecord(BaseRecord):
    recordType: Literal["NEWS"] = "NEWS"
    content: NewsContent


Record = Annotated[
    StartupRecord
    | ProductRecord
    | ResearchPaperRecord
    | JobRecord
    | NewsRecord,
    Field(discriminator="recordType"),
]


class EntityMappingRecord(Model):
    """Evidence-bearing row of the entity-mapping log."""

    raw_name: NonEmptyString
    normalized_key: NonEmptyString
    canonical_entity_id: NonEmptyString | None = None
    canonical_name: NonEmptyString | None = None
    status: Literal["RESOLVED", "UNRESOLVED", "MISSING"]
    method: NonEmptyString | None = None
    record_type: Literal["STARTUP", "PRODUCT", "RESEARCH_PAPER", "JOB", "NEWS"]
    source_url: HttpUrl
    source_text_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    evidence: NonEmptyString
    ownership_verification_status: Literal[
        "NAME_MATCH_ONLY",
        "NOT_VERIFIED",
        "SOURCE_UNKNOWN",
    ] = "NAME_MATCH_ONLY"
    ownership_verified: Literal[False] = False