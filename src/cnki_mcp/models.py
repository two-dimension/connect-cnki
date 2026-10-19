from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .config import StrictModel
from .core import require

PaperType = Literal["journal", "conference", "thesis", "book", "preprint", "correction", "retraction", "other"]


class ImportRights(StrictModel):
    basis: str = Field(default="", max_length=2000)
    allow_local_parse: bool = False
    allowed_recipients: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def needs_basis(self):
        require(not (self.allow_local_parse or self.allowed_recipients) or bool(self.basis.strip()),
                "POLICY_UNVERIFIED", "授权本地解析或内容输出时必须填写取得和处理依据。")
        return self


class PaperInput(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    authors: list[str] = Field(default_factory=list, max_length=100)
    year: int | None = Field(default=None, ge=1500, le=2100)
    paper_type: PaperType = "journal"
    source: str | None = Field(default=None, max_length=1000)
    source_id: str | None = Field(default=None, max_length=250)
    source_provider: str = Field(default="import", max_length=100)
    detail_url: str | None = Field(default=None, max_length=2000)
    doi: str | None = Field(default=None, max_length=250)
    abstract: str | None = Field(default=None, max_length=50000)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    volume: str | None = Field(default=None, max_length=100)
    issue: str | None = Field(default=None, max_length=100)
    pages: str | None = Field(default=None, max_length=100)
    citation_count: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list, max_length=100)
    retracted: bool | None = None
    version_type: str = Field(default="published", max_length=100)


class SearchQuery(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    field: Literal["subject", "title", "keywords", "author", "source"] = "subject"
    original_question: str = Field(default="", max_length=4000)


class SearchFilters(StrictModel):
    year_from: int | None = Field(default=None, ge=1500, le=2100)
    year_to: int | None = Field(default=None, ge=1500, le=2100)
    paper_types: list[PaperType] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def years(self):
        require(not (self.year_from and self.year_to) or self.year_from <= self.year_to,
                "INVALID_ARGUMENT", "起始年份不能大于结束年份。")
        return self


class ScreeningRules(StrictModel):
    enabled: bool = True
    mode: Literal["explore", "balanced", "strict"] = "balanced"
    discipline: Literal["economics", "computer_science", "electrical", "experimental_science", "theory"] = "computer_science"
    year_from: int | None = Field(default=None, ge=1500, le=2100)
    year_to: int | None = Field(default=None, ge=1500, le=2100)
    paper_types: list[PaperType] = Field(default_factory=list)
    venue_allowlist: list[str] = Field(default_factory=list, max_length=500)
    venue_list_version: str = Field(default="", max_length=200)
    minimum_citations: int = Field(default=0, ge=0)
    classic_version_ids: list[str] = Field(default_factory=list, max_length=500)
    exclude_confirmed_retractions: bool = True
    missing_strategy: Literal["review", "include"] = "review"
    fulltext_min_average: float = Field(default=1.5, ge=0, le=2)
    fulltext_min_coverage: float = Field(default=0.8, ge=0, le=1)


class DimensionRating(StrictModel):
    dimension: str = Field(min_length=1, max_length=100)
    score: int | None = Field(default=None, ge=0, le=2)
    applicable: bool = True
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default="", max_length=2000)


class ClaimInput(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    version_id: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    reading_level: Literal["metadata_only", "abstract_only", "fulltext"] = "fulltext"
    quotation: str | None = Field(default=None, max_length=6000)
