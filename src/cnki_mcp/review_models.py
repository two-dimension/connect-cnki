"""Typed, bounded contracts for the optional review workspace."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .config import StrictModel

Role = Literal["agent", "user"]
Level = Literal["metadata_only", "abstract_only", "fulltext"]


class ReviewPlanInput(StrictModel):
    goal: str = Field(min_length=1, max_length=4000)
    discipline: str = Field(default="general", max_length=100)
    genre: str = Field(default="related_work", max_length=100)
    reader_background: str = Field(default="", max_length=4000)
    subquestions: list[str] = Field(default_factory=list, max_length=50)
    scope: str = Field(default="", max_length=6000)
    time_range: str = Field(default="", max_length=500)
    languages: list[str] = Field(default_factory=list, max_length=20)
    paper_types: list[str] = Field(default_factory=list, max_length=20)
    inclusion: list[str] = Field(default_factory=list, max_length=100)
    exclusion: list[str] = Field(default_factory=list, max_length=100)
    seed_version_ids: list[str] = Field(default_factory=list, max_length=100)
    planned_queries: list[dict] = Field(default_factory=list, max_length=100)
    executed_query_ids: list[str] = Field(default_factory=list, max_length=100)
    synonyms: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    chapter_requirements: str = Field(default="", max_length=6000)
    classification_reason: str = Field(default="", max_length=4000)
    comparison_dimensions: list[str] = Field(default_factory=list, max_length=30)
    terminology: dict[str, str] = Field(default_factory=dict, max_length=200)
    citation_style: Literal["gbt-7714-2025", "gbt-7714-2015"] = "gbt-7714-2025"


class CorpusItemInput(StrictModel):
    version_id: str
    observation_id: str | None = None
    asset_id: str | None = None
    parse_artifact_id: str | None = None
    role: Literal["core", "direct", "background", "undecided"] = "undecided"
    subquestions: list[str] = Field(default_factory=list, max_length=50)


class EvidenceReference(StrictModel):
    version_id: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    observation_id: str | None = None
    quotation: str | None = Field(default=None, max_length=6000)


class SectionRange(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)
    verified_by_user: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.page_to < self.page_from:
            raise ValueError("reversed section pages")
        return self


class CardField(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    value: str | None = Field(default=None, max_length=10000)
    source_type: Literal["source_statement", "author_interpretation", "agent_synthesis", "user_judgment"] = "source_statement"
    state: Literal["unknown", "extracted", "needs_review"] = "unknown"
    references: list[EvidenceReference] = Field(default_factory=list, max_length=30)
    human_checked: bool = False


class PaperCardInput(StrictModel):
    version_id: str
    subquestions: list[str] = Field(default_factory=list, max_length=50)
    fields: list[CardField] = Field(default_factory=list, max_length=50)
    sections: list[SectionRange] = Field(default_factory=list, max_length=50)
    pending_questions: list[str] = Field(default_factory=list, max_length=100)
