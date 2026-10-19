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


class ClaimSourceInput(StrictModel):
    source_id: str | None = None
    relation: Literal["supports", "contradicts", "qualifies", "background"]
    reference: EvidenceReference
    conditions: str = Field(default="", max_length=4000)
    independence_group: str | None = Field(default=None, max_length=200)


class ReviewClaimInput(StrictModel):
    claim_id: str | None = None
    text: str = Field(min_length=1, max_length=10000)
    claim_type: Literal["finding", "synthesis", "gap"] = "finding"
    scope: str = Field(default="", max_length=4000)
    sources: list[ClaimSourceInput] = Field(default_factory=list, max_length=100)
    pending_questions: list[str] = Field(default_factory=list, max_length=100)


class ComparisonRow(StrictModel):
    version_id: str
    dimensions: dict[str, str | None] = Field(default_factory=dict, max_length=30)
    comparable: bool | None = None
    comparison_limits: str = Field(default="", max_length=4000)
    references: list[EvidenceReference] = Field(default_factory=list, max_length=30)


class SynthesisThemeInput(StrictModel):
    theme_id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    subquestion: str = Field(default="", max_length=4000)
    classification_reason: str = Field(default="", max_length=4000)
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    rows: list[ComparisonRow] = Field(default_factory=list, max_length=100)
    consensus: str = Field(default="", max_length=6000)
    disagreement: str = Field(default="", max_length=6000)
    contrary_evidence: str = Field(default="", max_length=6000)
    project_relation: str = Field(default="", max_length=4000)
    pending_questions: list[str] = Field(default_factory=list, max_length=100)


class SynthesisInput(StrictModel):
    subquestion: str = Field(min_length=1, max_length=4000)
    plan_id: str | None = None
    card_ids: list[str] = Field(default_factory=list, max_length=100)
    themes: list[SynthesisThemeInput] = Field(default_factory=list, max_length=50)
    claims: list[ReviewClaimInput] = Field(default_factory=list, max_length=200)
    search_scope: str = Field(default="current selected corpus", max_length=6000)


class ParagraphInput(StrictModel):
    paragraph_id: str | None = None
    text: str = Field(min_length=1, max_length=20000)
    claim_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewSectionInput(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=4000)
    thesis: str = Field(default="", max_length=6000)
    plan_id: str | None = None
    synthesis_ids: list[str] = Field(default_factory=list, max_length=100)
    card_ids: list[str] = Field(default_factory=list, max_length=100)
    paragraphs: list[ParagraphInput] = Field(default_factory=list, max_length=200)
    classification_reason: str = Field(default="", max_length=4000)
    comparison_dimensions: list[str] = Field(default_factory=list, max_length=30)
    supporting_arguments: list[str] = Field(default_factory=list, max_length=100)
    contrary_arguments: list[str] = Field(default_factory=list, max_length=100)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=100)
    previous_context: str = Field(default="", max_length=6000)
    next_context: str = Field(default="", max_length=6000)
    terminology: dict[str, str] = Field(default_factory=dict, max_length=200)
    established_conclusions: list[str] = Field(default_factory=list, max_length=100)
    open_questions: list[str] = Field(default_factory=list, max_length=100)
    editing_pass: Literal["draft", "facts", "structure", "language"] = "draft"
    allow_user_edit_changes: bool = False


class ClaimAssessment(StrictModel):
    claim_id: str
    status: Literal["fully_supported", "partially_supported", "contradicted", "insufficient_evidence", "not_assessed"]
    reason: str = Field(min_length=1, max_length=6000)
    references: list[EvidenceReference] = Field(default_factory=list, max_length=100)


class SentenceAssessment(StrictModel):
    sentence_id: str
    disposition: Literal["factual", "non_factual", "unresolved"]
    claim_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=4000)


class ReverseOutlineItem(StrictModel):
    paragraph_id: str
    main_point: str = Field(min_length=1, max_length=4000)
    function: str = Field(min_length=1, max_length=2000)
    connection: str = Field(default="", max_length=4000)


class ReviewCheckInput(StrictModel):
    review_type: Literal["support", "structure", "language", "human"] = "support"
    claim_assessments: list[ClaimAssessment] = Field(default_factory=list, max_length=200)
    sentence_assessments: list[SentenceAssessment] = Field(default_factory=list, max_length=500)
    reverse_outline: list[ReverseOutlineItem] = Field(default_factory=list, max_length=200)
    issues: list[str] = Field(default_factory=list, max_length=200)
