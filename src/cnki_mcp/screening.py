from __future__ import annotations

from datetime import UTC, datetime

from .core import Store, now, require
from .models import DimensionRating, ScreeningRules
from .policy import protected

TEMPLATES = {
    "economics": ["data_source", "sample_selection", "variables", "identification", "endogeneity", "robustness"],
    "computer_science": ["data_split", "leakage", "baselines", "ablation", "randomness", "compute", "reproducibility"],
    "electrical": ["simulation_vs_measurement", "conditions", "parameters", "measurement_error", "benchmarks"],
    "experimental_science": ["controls", "calibration", "replication", "uncertainty", "data_support"],
    "theory": ["definitions", "assumptions", "proof", "lemmas", "scope"],
}


class Screening:
    def __init__(self, store: Store):
        self.store = store

    def run(self, paper: dict, policy: dict, stage: str, ratings: list[DimensionRating]) -> dict:
        rules = ScreeningRules.model_validate(policy["rules"])
        reasons, missing, dimensions = [], [], []
        result, coverage, average = "recommended", None, None
        if rules.enabled:
            if rules.exclude_confirmed_retractions and paper.get("retracted"):
                reasons.append("confirmed_retraction")
            if paper["id"] not in rules.classic_version_ids:
                if (rules.year_from or rules.year_to) and paper.get("year") is None:
                    missing.append("year")
                elif paper.get("year") and ((rules.year_from and paper["year"] < rules.year_from)
                                           or (rules.year_to and paper["year"] > rules.year_to)):
                    reasons.append("outside_year_range")
            if rules.paper_types and paper["paper_type"] not in rules.paper_types:
                reasons.append("paper_type")
            if rules.venue_allowlist and paper.get("source") not in rules.venue_allowlist:
                if rules.mode == "strict":
                    if paper.get("source"):
                        reasons.append("venue_allowlist")
                    else:
                        missing.append("source")
            if rules.minimum_citations and paper.get("citation_count") is None:
                missing.append("citation_count")
            elif rules.minimum_citations and paper["citation_count"] < rules.minimum_citations:
                if paper.get("year") and paper["year"] >= datetime.now(UTC).year - 2:
                    missing.append("citation_signal_for_recent_paper")
                else:
                    reasons.append("below_user_citation_threshold")
            if stage == "fulltext":
                expected = TEMPLATES[rules.discipline]
                supplied = {rating.dimension: rating for rating in ratings}
                require(len(supplied) == len(ratings) and set(supplied) <= set(expected),
                        "INVALID_ARGUMENT", "评价项重复或不属于学科模板。")
                applicable, completed, scores = 0, 0, []
                for name in expected:
                    rating = supplied.get(name) or DimensionRating(dimension=name)
                    evidence = [self.store.get(item, "evidence") for item in rating.evidence_ids]
                    require(all(e["version_id"] == paper["id"] for e in evidence),
                            "EVIDENCE_MISMATCH", "评价引用了其他文献的证据。")
                    if not rating.applicable:
                        require(bool(rating.reason and evidence), "EVIDENCE_REQUIRED", "不适用判断必须给出依据与证据。")
                    else:
                        applicable += 1
                        if rating.score is not None:
                            require(bool(evidence), "EVIDENCE_REQUIRED", "评分必须绑定全文证据。")
                            completed += 1
                            scores.append(rating.score)
                        else:
                            missing.append(name)
                    dimensions.append(rating.model_dump())
                coverage = completed / applicable if applicable else None
                average = sum(scores) / len(scores) if scores else None
                if rules.mode == "strict" and not missing and applicable:
                    if average is None or average < rules.fulltext_min_average or 0 in scores:
                        reasons.append("method_threshold")
                    if coverage < rules.fulltext_min_coverage:
                        reasons.append("evidence_coverage")
            if reasons:
                result = "excluded" if rules.mode != "explore" or "confirmed_retraction" in reasons else "conditional"
            elif missing:
                result = "pending_review" if rules.missing_strategy == "review" or (
                    stage == "fulltext" and rules.mode == "strict") else "conditional"
        else:
            reasons.append("screening_disabled")
        decision = self.store.put("decision", {
            "version_id": paper["id"], "policy_id": policy["id"], "stage": stage,
            "result": result, "reasons": reasons, "missing": missing, "dimensions": dimensions,
            "evidence_coverage": coverage, "average_score": average, "at": now(),
            "assessment_source": "agent_supplied_evidence_ratings" if stage == "fulltext" else "deterministic_metadata_rules",
            "dimensions_summary": {"relevance": "not_assessed", "method": "evaluated" if dimensions else "unknown",
                                   "impact": "citation_signal_only", "evidence_completeness": coverage},
        })
        return {**decision, "dimensions": protected(dimensions, "fulltext", paper["rights"])}
