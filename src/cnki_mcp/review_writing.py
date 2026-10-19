from __future__ import annotations

import re

from .core import digest, require, uid
from .review_models import ParagraphInput, ReviewCheckInput, ReviewSectionInput, SynthesisInput
from .review_workspace import ReviewWorkspace
from .writing import WRITING_RULES, WRITING_VERSION


class ReviewWriting:
    def __init__(self, workspace: ReviewWorkspace):
        self.workspace, self.store = workspace, workspace.store

    def dependencies(self, project_id, snapshot, plan_id=None, card_ids=None, synthesis_ids=None):
        deps, rights, records = [snapshot["id"]], [], []
        for kind, ids in (("review_plan", [plan_id] if plan_id else []), ("paper_card", card_ids or []),
                          ("synthesis", synthesis_ids or [])):
            for record_id in ids:
                record = self.workspace.get(project_id, record_id, kind)
                require(kind == "review_plan" or record["content"]["snapshot_id"] == snapshot["id"],
                        "EVIDENCE_MISMATCH", "依赖成果使用了不同语料快照。")
                deps.append(record["id"])
                rights.extend(record["rights"])
                records.append(record)
        return deps, rights, records

    @staticmethod
    def stable_id(proposed, previous_ids, prefix):
        require(proposed is None or proposed in previous_ids, "REVISION_CONFLICT", "稳定内部标识须来自该成果的前一版本。")
        return proposed or uid(prefix)

    def save_synthesis(self, project_id: str, snapshot_id: str, synthesis: SynthesisInput, **revision):
        snapshot = self.workspace.corpus(project_id, snapshot_id)
        dependencies, rights, _ = self.dependencies(project_id, snapshot, synthesis.plan_id, synthesis.card_ids)
        previous = self.workspace.get(project_id, revision["entity_id"], "synthesis") if revision.get("entity_id") else None
        previous_claims = {c["claim_id"]: c for c in previous["content"]["claims"]} if previous else {}
        previous_themes = {t["theme_id"] for t in previous["content"]["themes"]} if previous else set()
        claims, themes = [], []
        for claim in synthesis.claims:
            claim_id = self.stable_id(claim.claim_id, previous_claims, "claim")
            require(claim.claim_type != "gap" or bool(claim.scope.strip()), "INVALID_ARGUMENT", "研究缺口必须明确材料和检索范围。")
            sources, support_groups = [], []
            old_sources = {s["source_id"] for s in previous_claims.get(claim_id, {}).get("sources", [])}
            for source in claim.sources:
                refs, source_rights, deps = self.workspace.references(snapshot, [source.reference])
                rights.extend(source_rights)
                dependencies.extend(deps)
                item = self.workspace.item(snapshot, source.reference.version_id)
                sources.append({**source.model_dump(), "source_id": self.stable_id(source.source_id, old_sources, "claimsource"),
                                "reference": refs[0]})
                if source.relation == "supports":
                    support_groups.append({"work_id": item["work_id"], "sha256": item["sha256"],
                                           "declared": source.independence_group})
            # Connected components: shared work, bytes OR explicitly shared data are not independent supports.
            groups = []
            for source in support_groups:
                matches = [g for g in groups if any(any(source[k] and source[k] == s[k] for k in source) for s in g)]
                combined = [source] + [s for g in matches for s in g]
                groups = [g for g in groups if g not in matches] + [combined]
            claims.append({**claim.model_dump(), "claim_id": claim_id, "sources": sources,
                "independent_support_groups": len(groups), "independence_status": "known_relationships_only",
                "support_status": "not_assessed"})
        for theme in synthesis.themes:
            rows = []
            for row in theme.rows:
                self.workspace.item(snapshot, row.version_id)
                require(all(r.version_id == row.version_id for r in row.references), "EVIDENCE_MISMATCH", "比较行的证据须属于该论文。")
                refs, source_rights, deps = self.workspace.references(snapshot, row.references)
                rights.extend(source_rights)
                dependencies.extend(deps)
                rows.append({**row.model_dump(), "references": refs})
            themes.append({**theme.model_dump(), "theme_id": self.stable_id(theme.theme_id, previous_themes, "theme"), "rows": rows})
        require(len({c["claim_id"] for c in claims}) == len(claims) and len({t["theme_id"] for t in themes}) == len(themes),
                "INVALID_ARGUMENT", "论断或主题标识重复。")
        # Narrative comparisons inherit the corpus too, including unlinked/contrary source material.
        rights.extend(self.corpus_rights(snapshot))
        record = self.workspace.revision(project_id, "synthesis", {**synthesis.model_dump(), "snapshot_id": snapshot["id"],
            "claims": claims, "themes": themes, "writing_rules_version": WRITING_VERSION},
            dependencies=dependencies, rights=rights, **revision)
        return self.workspace.present(record)

    @staticmethod
    def corpus_rights(snapshot):
        return [i["fulltext_rights"] if i["reading_level"] == "fulltext" else i["metadata_rights"] for i in snapshot["content"]["items"]]

    @staticmethod
    def fact_signature(text):
        return {"numbers": re.findall(r"[-+]?\d+(?:\.\d+)?(?:%|％)?", text),
                "units": re.findall(r"%|％|\b(?:kg|mg|g|km|mm|cm|m|Hz|GHz|MHz|W|kW|s|ms|percent)\b|摄氏度|万元|亿元", text),
                "negations": re.findall(r"不|未|无|并非|\bnot\b|\bnever\b", text),
                "uncertainties": re.findall(r"可能|或许|推测|\bmay\b|\bmight\b", text)}

    def save_section(self, project_id: str, snapshot_id: str, body: str, section: ReviewSectionInput, **revision):
        snapshot = self.workspace.corpus(project_id, snapshot_id)
        dependencies, rights, records = self.dependencies(project_id, snapshot, section.plan_id, section.card_ids, section.synthesis_ids)
        claims = {c["claim_id"]: c for r in records if r["kind"] == "synthesis" for c in r["content"]["claims"]}
        previous = self.workspace.get(project_id, revision["entity_id"], "review_section") if revision.get("entity_id") else None
        old_paragraphs = {p["paragraph_id"]: p for p in previous["content"]["paragraphs"]} if previous else {}
        paragraphs = section.paragraphs or [ParagraphInput(text=p) for p in body.split("\n\n") if p.strip()]
        require("\n\n".join(p.text for p in paragraphs) == body, "INVALID_ARGUMENT", "段落文本以空行连接后必须与正文一致。")
        used_ids, saved, inventory = set(), [], []
        for paragraph in paragraphs:
            proposed = paragraph.paragraph_id
            if proposed is None:
                matches = [p for p in old_paragraphs.values() if p["text"] == paragraph.text and p["paragraph_id"] not in used_ids]
                proposed = matches[0]["paragraph_id"] if len(matches) == 1 else None
            paragraph_id = self.stable_id(proposed, old_paragraphs, "paragraph")
            require(paragraph_id not in used_ids, "INVALID_ARGUMENT", "段落标识重复。")
            used_ids.add(paragraph_id)
            old = old_paragraphs.get(paragraph_id)
            require(not old or old["authored_by"] != "user" or old["text"] == paragraph.text
                    or revision.get("actor_role") == "user" or section.allow_user_edit_changes,
                    "USER_EDIT_CONFLICT", "该段包含人工修改，请保留或明确提交针对人工段落的修订。")
            require(set(paragraph.claim_ids) <= claims.keys(), "UNKNOWN_CLAIM", "段落关联的论断不在所选综合版本中。")
            saved.append({**paragraph.model_dump(), "paragraph_id": paragraph_id,
                          "authored_by": old["authored_by"] if old and old["text"] == paragraph.text else revision.get("actor_role", "agent")})
            previous_sentences = {s["text"]: s["sentence_id"] for s in previous["content"]["sentence_inventory"]
                                  if s["paragraph_id"] == paragraph_id} if previous else {}
            for match in re.finditer(r".+?(?:[。！？!?]+|(?<!\d)\.(?=\s|$)|$)", paragraph.text, re.S):
                text = match.group().strip()
                if not text:
                    continue
                mapped = [c for c in paragraph.claim_ids if claims[c]["text"] in text]
                inventory.append({"sentence_id": previous_sentences.get(text) or uid("sentence"), "paragraph_id": paragraph_id,
                    "text": text, "offset": match.start(), "mapped_claim_ids": mapped, "disposition": "unresolved",
                    "status": "needs_review", "risk_signals": [name for name, values in self.fact_signature(text).items() if values]
                        + (["causal_or_gap_language"] if re.search(r"导致|因果|首次|无人|所有|多数|caus|first|all studies", text, re.I) else [])})
        missing_user = [p for p in old_paragraphs.values() if p["authored_by"] == "user" and p["paragraph_id"] not in used_ids]
        require(not missing_user or revision.get("actor_role") == "user" or section.allow_user_edit_changes,
                "USER_EDIT_CONFLICT", "未显式处理的人工段落不能删除。")
        cited = set(re.findall(r"\[(version_[A-Za-z0-9_]+)\]", body))
        allowed = {i["version_id"] for i in snapshot["content"]["items"]}
        mapped_claims = {c for p in saved for c in p["claim_ids"]}
        source_versions = {s["reference"]["version_id"] for c in mapped_claims for s in claims[c]["sources"]}
        signature = self.fact_signature(body)
        validation = {"identity_valid": not (cited - allowed), "unknown_citations": sorted(cited - allowed),
            "unmapped_citations": sorted(cited - source_versions), "uncited_claim_sources": sorted(source_versions - cited),
            "sentence_count": len(inventory), "sentence_inventory_complete": True,
            "unmapped_sentence_ids": [s["sentence_id"] for s in inventory if not s["mapped_claim_ids"]],
            "substantive_fact_coverage": "not_certified", "support_status": "not_certified", "requires_review": True,
            "fact_signature_changed": bool(previous and previous["content"]["fact_signature"] != signature),
            "language_pass_requires_fact_recheck": bool(previous and section.editing_pass == "language"
                                                       and previous["content"]["fact_signature"] != signature)}
        record = self.workspace.revision(project_id, "review_section", {**section.model_dump(), "snapshot_id": snapshot["id"],
            "body": body, "paragraphs": saved, "sentence_inventory": inventory, "claims": list(claims.values()),
            "validation": validation, "fact_signature": signature, "writing_rules_version": WRITING_VERSION,
            "writing_rules": WRITING_RULES}, dependencies=dependencies, rights=[*rights, *self.corpus_rights(snapshot)], **revision)
        return self.workspace.present(record)

    def save_check(self, project_id: str, section_id: str, check: ReviewCheckInput, **revision):
        section = self.workspace.get(project_id, section_id, "review_section")
        snapshot = self.workspace.corpus(project_id, section["content"]["snapshot_id"])
        actor = revision.get("actor_role", "agent")
        require(check.review_type != "human" or actor == "user", "INVALID_ARGUMENT", "人工核查记录需用户角色。")
        claims = {c["claim_id"] for c in section["content"]["claims"]}
        sentences = {s["sentence_id"] for s in section["content"]["sentence_inventory"]}
        paragraphs = {p["paragraph_id"] for p in section["content"]["paragraphs"]}
        dependencies, rights = [section["id"]], list(section["rights"])
        assessments = []
        require(len({a.claim_id for a in check.claim_assessments}) == len(check.claim_assessments)
                and len({a.sentence_id for a in check.sentence_assessments}) == len(check.sentence_assessments),
                "INVALID_ARGUMENT", "核查对象重复。")
        for assessment in check.claim_assessments:
            require(assessment.claim_id in claims, "UNKNOWN_CLAIM", "核查论断不在该章节修订中。")
            require(assessment.status not in {"fully_supported", "partially_supported", "contradicted"} or assessment.references,
                    "MISSING_EVIDENCE", "支持、部分支持或矛盾意见需附上下文证据。")
            refs, source_rights, deps = self.workspace.references(snapshot, assessment.references)
            dependencies.extend(deps)
            rights.extend(source_rights)
            assessments.append({**assessment.model_dump(), "references": refs})
        for assessment in check.sentence_assessments:
            require(assessment.sentence_id in sentences and set(assessment.claim_ids) <= claims,
                    "UNKNOWN_CLAIM", "句子或论断不属于该章节版本。")
            require(assessment.disposition != "factual" or assessment.claim_ids, "MISSING_EVIDENCE", "事实性句子需映射论断。")
        require(all(o.paragraph_id in paragraphs for o in check.reverse_outline), "INVALID_ARGUMENT", "反向提纲段落不属于该正文。")
        accounted = {s.sentence_id for s in check.sentence_assessments if s.disposition != "unresolved"}
        content = {**check.model_dump(), "section_revision_id": section["id"], "snapshot_id": snapshot["id"],
            "claim_assessments": assessments, "opinion_origin": "user_declared" if actor == "user" else "caller_model",
            "support_status": "not_certified", "sentence_inventory_accounted": len(accounted) == len(sentences),
            "unreviewed_sentence_ids": sorted(sentences - accounted), "body_digest": digest(section["content"]["body"]),
            "writing_rules_version": WRITING_VERSION}
        return self.workspace.present(self.workspace.revision(project_id, "review_check", content,
            dependencies=dependencies, rights=rights, **revision))
