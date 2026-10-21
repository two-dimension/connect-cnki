from __future__ import annotations

from datetime import UTC, datetime

from .core import AppError, Store, now, require
from .models import DimensionRating, ScreeningRules
from .policy import protected_all

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

    def requirements(self, decision: dict) -> tuple[list[dict], list[str]]:
        """Resolve historical IDs again: source permissions can change after screening."""
        requirements, missing = [], []
        for dimension in decision.get("dimensions", []):
            ids = dimension.get("evidence_ids", [])
            if not ids and (dimension.get("reason") or dimension.get("score") is not None
                            or not dimension.get("applicable", True)):
                missing.append(dimension["dimension"])
            for evidence_id in ids:
                try:
                    evidence = self.store.get(evidence_id, "evidence")
                    require(evidence["version_id"] == decision["version_id"], "EVIDENCE_MISMATCH", "证据版本不符。")
                    asset = self.store.get(evidence["asset_id"], "asset")
                    require(asset["version_id"] == evidence["version_id"], "EVIDENCE_MISMATCH", "证据文件版本不符。")
                    requirements.extend({"content_class": "fulltext", "rights": rights}
                                        for rights in (evidence["rights"], asset["rights"]))
                except (AppError, KeyError):
                    missing.append(evidence_id)
        if missing:
            requirements.append({"content_class": "fulltext", "rights": {}})
        return requirements, sorted(set(missing))

    def present(self, decision: dict) -> dict:
        _, missing = self.requirements(decision)
        dimensions = [protected_all(d, self.requirements({**decision, "dimensions": [d]})[0])
                      for d in decision.get("dimensions", [])]
        return {**decision, "dimensions": dimensions,
                "dependency_status": "missing" if missing else "resolved", "missing_dependencies": missing}

    def protect_override(self, decision: dict, reason: str) -> dict:
        requirements, _ = self.requirements(decision)
        paper = self.store.get(decision["version_id"], "version")
        requirements.append({"content_class": "review", "rights": paper["rights"]})
        return protected_all(reason, requirements)

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
            "evidence_dependencies": [{"dimension": d["dimension"], "evidence_id": evidence_id,
                "rights_source_id": self.store.get(evidence_id, "evidence")["asset_id"]}
                for d in dimensions for evidence_id in d["evidence_ids"]],
            "evidence_coverage": coverage, "average_score": average, "at": now(),
            "assessment_source": "agent_supplied_evidence_ratings" if stage == "fulltext" else "deterministic_metadata_rules",
            "dimensions_summary": {"relevance": "not_assessed", "method": "evaluated" if dimensions else "unknown",
                                   "impact": "citation_signal_only", "evidence_completeness": coverage},
        })
        return self.present(decision)
