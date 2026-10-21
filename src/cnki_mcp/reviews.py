from __future__ import annotations

import re

from .core import Store, now, require
from .library import Library
from .models import ClaimInput
from .parsing import Documents
from .policy import protected


class Reviews:
    def __init__(self, store: Store, library: Library, documents: Documents):
        self.store, self.library, self.documents = store, library, documents

    def prepare(self, project_id: str, question: str, version_ids: list[str] | None):
        project = self.store.get(project_id, "project")
        papers = self.store.papers(project_id)
        if version_ids:
            require(set(version_ids) <= {p["id"] for p in papers}, "PROJECT_MISMATCH", "文献不属于该项目。")
            papers = [p for p in papers if p["id"] in version_ids]
        matrix = []
        remaining_chars = self.documents.settings.max_read_chars
        evidence_truncated = False
        for paper in papers:
            parses = self.store.find("parse", version_id=paper["id"])
            spans = self.store.find("evidence", version_id=paper["id"])
            level = "fulltext" if spans else "abstract_only" if paper.get("abstract") else "metadata_only"
            selected_spans = []
            for span in spans[:3]:
                if len(span["text"]) > remaining_chars:
                    evidence_truncated = True
                    continue
                selected_spans.append(self.documents.span(span))
                remaining_chars -= len(span["text"])
            matrix.append({
                "version_id": paper["id"], "bibliography": self.library.present(paper), "reading_level": level,
                "research_question": None, "object": None, "methods": None, "data_or_experiments": None,
                "results": None, "limitations": None, "parse_artifacts": parses,
                "evidence": selected_spans,
                "needs_agent_analysis": True, "support_status": "not_assessed",
            })
        return {"project_id": project_id, "question": question or project["question"], "matrix": matrix,
                "evidence_truncated": evidence_truncated,
                "outline": ["研究问题与检索范围", "研究方法与条件", "结果共识与分歧", "局限与证据不足", "研究空间"],
                "instructions": "由调用方 Agent 综合证据；每个实质论断通过 ClaimInput 绑定版本和证据。正文引用使用 [version_id]。",
                "model_calls": 0}

    def save(self, project_id: str, body: str, claims: list[ClaimInput]):
        project_papers = {p["id"]: p for p in self.store.papers(project_id)}
        checks = []
        all_rights = []
        for claim in claims:
            exists = claim.version_id in project_papers
            evidence, errors = [], []
            if not exists:
                errors.append("unknown_or_out_of_project_version")
                all_rights.append({})
            for evidence_id in claim.evidence_ids:
                try:
                    span = self.store.get(evidence_id, "evidence")
                    if span["version_id"] != claim.version_id:
                        errors.append("evidence_version_mismatch")
                    else:
                        evidence.append(span)
                        all_rights.append(span["rights"])
                except Exception:
                    errors.append("unknown_evidence")
            if claim.reading_level == "fulltext" and not evidence:
                errors.append("missing_fulltext_evidence")
            if claim.reading_level == "abstract_only" and exists and not project_papers[claim.version_id].get("abstract"):
                errors.append("missing_abstract")
            if claim.quotation and not any(claim.quotation in e["text"] for e in evidence):
                errors.append("quotation_not_found")
            if claim.text not in body:
                errors.append("claim_not_in_body")
            if exists and claim.reading_level != "fulltext":
                all_rights.append(project_papers[claim.version_id]["rights"])
            checks.append({"version_id": claim.version_id, "evidence_ids": [e["id"] for e in evidence],
                           "reading_level": claim.reading_level, "identity_valid": exists and not any(
                               e in errors for e in ("evidence_version_mismatch", "unknown_evidence")),
                           "quotation_verified": bool(claim.quotation) and "quotation_not_found" not in errors,
                           "errors": errors, "support_check": "human_or_model_review_required"})
        cited = set(re.findall(r"\[(version_[A-Za-z0-9_]+)\]", body))
        unknown = sorted(cited - project_papers.keys())
        unmapped = sorted(cited - {claim.version_id for claim in claims})
        # Semantic support cannot be certified by ID/substring checks.
        validation = {"identity_valid": not unknown and all(x["identity_valid"] for x in checks),
                      "unknown_citations": unknown, "unmapped_citations": unmapped,
                      "claims": checks, "support_status": "not_certified",
                      "requires_review": True, "substantive_fact_coverage": "not_automatically_measured",
                      "no_claims": not bool(claims)}
        review = self.store.put("review", {
            "project_id": project_id, "body": body, "claims": [c.model_dump() for c in claims],
            "validation": validation, "rights": all_rights, "at": now(), "state": "draft",
        })
        return {"review_id": review["id"], "state": "draft", "validation": validation,
                "body": self.protect_review(body, all_rights)}

    @staticmethod
    def protect_review(body: str, all_rights: list[dict]):
        value = body
        # A synthesis inherits all source restrictions. No claims means unknown rights.
        for rights in all_rights or [{}]:
            value = protected(value, "review", rights)
        return value
