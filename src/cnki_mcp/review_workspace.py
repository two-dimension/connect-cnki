from __future__ import annotations

from .core import Store, digest, now, require, uid
from .library import Library
from .policy import PolicyGate, protected_all
from .review_models import CorpusItemInput, EvidenceReference, PaperCardInput, ReviewPlanInput

ARTIFACT_KINDS = {"review_plan", "corpus_snapshot", "paper_card", "synthesis", "review_section", "review_check", "reading_progress"}


class ReviewWorkspace:
    """Project-scoped immutable revisions; only head pointers and project choices change."""

    def __init__(self, store: Store, library: Library, gate: PolicyGate):
        self.store, self.library, self.gate = store, library, gate

    def project(self, project_id: str, writing: bool = False):
        project = self.store.get(project_id, "project")
        require(not writing or project.get("review_assistance_enabled", False), "REVIEW_ASSISTANCE_DISABLED",
                "该项目的增强综述辅助尚未启用。", "明确选择后调用 set_review_assistance(project_id, true)。")
        return project

    def records(self, project_id: str, kind: str):
        # Indexed scope lookup, without loading other projects' source content.
        import json
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT body FROM records WHERE kind=? AND json_extract(body,'$.project_id')=? ORDER BY created_at,rowid",
            (kind, project_id))]

    def get(self, project_id: str, record_id: str, kind: str | None = None):
        self.project(project_id)
        record = self.store.get(record_id)
        if record.get("artifact_kind") and record.get("current_revision_id"):
            record = self.store.get(record["current_revision_id"])
        require(record.get("project_id") == project_id, "PROJECT_MISMATCH", "综述记录不属于指定项目。")
        require(record.get("kind") in ARTIFACT_KINDS and (kind is None or record["kind"] == kind),
                "INVALID_ARGUMENT", "综述记录类型不匹配。")
        return record

    def heads(self, project_id: str, kind: str | None = None):
        return [self.store.get(h["current_revision_id"]) for h in self.records(project_id, "review_head")
                if kind is None or h["artifact_kind"] == kind]

    def cards_for_snapshot(self, project_id: str, snapshot_id: str):
        cards = []
        for card in self.heads(project_id, "paper_card"):
            while card["content"]["snapshot_id"] != snapshot_id and card.get("parent_revision_id"):
                card = self.get(project_id, card["parent_revision_id"], "paper_card")
            if card["content"]["snapshot_id"] == snapshot_id:
                cards.append(card)
        return cards

    def revision(self, project_id: str, kind: str, content: dict, *, entity_id: str | None = None,
                 parent_revision_id: str | None = None, actor_role: str = "agent", reason: str = "initial",
                 dependencies: list[str] | None = None, rights: list[dict] | None = None):
        self.project(project_id, writing=True)
        require(kind in ARTIFACT_KINDS, "INVALID_ARGUMENT", "不支持的综述记录类型。")
        if entity_id:
            head = self.store.get(entity_id, "review_head")
            require(head["project_id"] == project_id and head["artifact_kind"] == kind,
                    "PROJECT_MISMATCH", "修订目标不属于该项目或类型。")
            require(parent_revision_id == head["current_revision_id"], "REVISION_CONFLICT",
                    "父版本已变化；请读取最新版本后合并修改。")
        else:
            require(parent_revision_id is None, "REVISION_CONFLICT", "新记录不能指定父版本。")
            entity_id = uid("reviewhead")
        dependencies = sorted(set(dependencies or []))
        for source in dependencies:
            self.store.get(source)
        record = self.store.put(kind, {"project_id": project_id, "kind": kind, "entity_id": entity_id,
            "parent_revision_id": parent_revision_id, "content": content, "actor_role": actor_role,
            "actor_provenance": "caller_declared", "reason": reason, "at": now(),
            "dependencies": dependencies, "rights": list({digest(r): r for r in rights or []}.values()), "content_digest": digest(content)})
        self.store.put("review_head", {"project_id": project_id, "artifact_kind": kind,
            "current_revision_id": record["id"]}, entity_id)
        for source in dependencies:
            self.store.db.execute("INSERT INTO review_dependencies VALUES (?,?,?,?)",
                                  (project_id, record["id"], source, "uses"))
        if parent_revision_id:
            self.invalidate(project_id, parent_revision_id, record["id"], "source_revision_changed")
        return record

    def invalidate(self, project_id: str, old_id: str, new_id: str, reason: str):
        affected, pending = set(), [old_id]
        while pending:
            source = pending.pop()
            for row in self.store.db.execute(
                    "SELECT dependent_id FROM review_dependencies WHERE project_id=? AND source_id=?", (project_id, source)):
                if row[0] not in affected:
                    affected.add(row[0])
                    pending.append(row[0])
        for record_id in affected:
            self.store.put("review_invalidation", {"project_id": project_id, "affected_id": record_id,
                "source_id": old_id, "replacement_id": new_id, "reason": reason, "at": now()})
        return sorted(affected)

    def present(self, record: dict):
        header = {k: v for k, v in record.items() if k not in {"rights", "content", "reason"}}
        header["current_dependency_status"] = "outdated" if self.is_outdated(record) else "unchanged"
        content = {"value": record["content"], "change_reason": record["reason"]}
        if record["kind"] == "corpus_snapshot":
            content["value"] = {**record["content"], "items": [self.present_item(i) for i in record["content"]["items"]]}
        elif record["kind"] not in {"review_plan", "reading_progress"}:
            content = self.protect_content(record, content)
        return {**header, "content": content}

    def is_outdated(self, record):
        invalidated = {r["affected_id"] for r in self.records(record["project_id"], "review_invalidation")}
        pending, seen = [record["id"]], set()
        while pending:
            record_id = pending.pop()
            if record_id in invalidated:
                return True
            if record_id not in seen:
                seen.add(record_id)
                dependency = self.store.get(record_id)
                if dependency.get("kind") == "corpus_snapshot" and any(
                        self.store.get(i["version_id"], "version")["work_id"] != i["work_id"]
                        for i in dependency["content"]["items"]):
                    return True
                pending.extend(dependency.get("dependencies", []))
        return False

    def requirements(self, record):
        levels = []
        if record["kind"] == "paper_card":
            levels = [r["reading_level"] for f in record["content"]["fields"] for r in f["references"]]
        if not levels and record["content"].get("snapshot_id"):
            snapshot = self.get(record["project_id"], record["content"]["snapshot_id"], "corpus_snapshot")
            levels = [i["reading_level"] for i in snapshot["content"]["items"]]
        classes = {"review"} | {"fulltext" if level == "fulltext" else "abstract" for level in levels if level != "metadata_only"}
        return [{"content_class": content_class, "rights": rights}
                for rights in record["rights"] or [{}] for content_class in sorted(classes)]

    def protect_content(self, record, content):
        return protected_all(content, self.requirements(record))

    def present_item(self, item: dict):
        clean = {k: v for k, v in item.items() if k not in {"bibliography", "metadata_rights", "fulltext_rights"}}
        clean["bibliography"] = self.library.present({**item["bibliography"], "rights": item["metadata_rights"]})
        return clean

    def set_enabled(self, project_id: str, enabled: bool):
        project = self.project(project_id)
        previous = project.get("review_assistance_enabled", False)
        generation = project.get("review_generation", 0) + int(previous and not enabled)
        self.store.put("project", {**project, "review_assistance_enabled": enabled, "review_generation": generation})
        jobs = [j["id"] for j in self.store.all("job") if j["arguments"].get("project_id") == project_id
                and j["operation"] in {"prepare_review_v2", "prepare_synthesis"}
                and j["state"] in {"queued", "running", "waiting_for_user"}]
        return {"project_id": project_id, "enabled": enabled, "review_generation": generation,
                "review_jobs": jobs, "job_action": "resume_explicitly" if enabled else "pause_at_checkpoint",
                "history_preserved": True, "base_jobs_affected": False}

    def save_plan(self, project_id: str, plan: ReviewPlanInput, **revision):
        self.project(project_id, writing=True)
        ids = {p["id"] for p in self.store.papers(project_id)}
        require(set(plan.seed_version_ids) <= ids, "PROJECT_MISMATCH", "种子文献不属于该项目。")
        for query_id in plan.executed_query_ids:
            query = self.store.get(query_id, "query")
            require(query["snapshot"]["project_id"] == project_id, "PROJECT_MISMATCH", "检索记录不属于该项目。")
        return self.present(self.revision(project_id, "review_plan", plan.model_dump(),
            dependencies=[*plan.seed_version_ids, *plan.executed_query_ids], **revision))

    def snapshot(self, project_id: str, items: list[CorpusItemInput], plan_id: str | None = None):
        project = self.project(project_id, writing=True)
        papers = {p["id"]: p for p in self.store.papers(project_id)}
        require(items and len({i.version_id for i in items}) == len(items), "INVALID_ARGUMENT", "快照需包含不重复的发表版本。")
        dependencies, selected = [], []
        if plan_id:
            plan = self.get(project_id, plan_id, "review_plan")
            dependencies.append(plan["id"])
        for item in items:
            require(item.version_id in papers, "PROJECT_MISMATCH", "快照文献不属于该项目。")
            paper = papers[item.version_id]
            observations = [o for o in self.store.all("observation") if o["version_id"] == paper["id"]]
            observation = self.store.get(item.observation_id, "observation") if item.observation_id else observations[-1]
            require(observation["version_id"] == paper["id"], "EVIDENCE_MISMATCH", "题录观测不属于该发表版本。")
            parses = [p for p in self.store.all("parse") if p["version_id"] == paper["id"]
                      and (not item.asset_id or p["asset_id"] == item.asset_id)]
            choices = [s for s in self.store.all("selection") if s["version_id"] == paper["id"]]
            artifact = self.store.get(item.parse_artifact_id, "parse") if item.parse_artifact_id else (
                self.store.get(choices[0]["parse_artifact_id"], "parse") if choices and not item.asset_id else parses[0] if parses else None)
            asset = self.store.get(item.asset_id, "asset") if item.asset_id else (
                self.store.get(artifact["asset_id"], "asset") if artifact else None)
            if asset:
                require(asset["version_id"] == paper["id"], "EVIDENCE_MISMATCH", "文件不属于该发表版本。")
            if artifact:
                require(artifact["version_id"] == paper["id"] and asset and artifact["asset_id"] == asset["id"]
                        and artifact["sha256"] == asset["sha256"], "EVIDENCE_MISMATCH", "文件与解析快照不一致。")
            bibliography = {**paper, **observation["fields"], "id": paper["id"], "work_id": paper["work_id"]}
            bibliography.pop("rights", None)
            spans = [e for e in self.store.all("evidence") if artifact and e["parse_artifact_id"] == artifact["id"]]
            level = "fulltext" if any(e["text"].strip() for e in spans) else "abstract_only" if bibliography.get("abstract") else "metadata_only"
            selected.append({"version_id": paper["id"], "work_id": paper["work_id"], "observation_id": observation["id"],
                "bibliography": bibliography, "metadata_rights": observation["rights"],
                "fulltext_rights": asset["rights"] if asset else {}, "asset_id": asset["id"] if asset else None,
                "parse_artifact_id": artifact["id"] if artifact else None, "sha256": asset["sha256"] if asset else None,
                "reading_level": level, "quality": artifact["quality"] if artifact else "not_parsed",
                "role": item.role, "subquestions": item.subquestions})
            dependencies.extend([paper["id"], observation["id"], *([asset["id"]] if asset else []), *([artifact["id"]] if artifact else [])])
        record = self.revision(project_id, "corpus_snapshot", {"items": selected, "plan_id": dependencies[0] if plan_id else None},
                               dependencies=dependencies)
        old_id = project.get("review_snapshot_id")
        self.store.put("project", {**project, "review_snapshot_id": record["id"]})
        if old_id and old_id != record["id"]:
            self.invalidate(project_id, old_id, record["id"], "selected_snapshot_changed")
        return self.present(record)

    def corpus(self, project_id: str, snapshot_id: str | None):
        project = self.project(project_id)
        snapshot_id = snapshot_id or project.get("review_snapshot_id")
        require(snapshot_id, "CORPUS_SNAPSHOT_REQUIRED", "请先显式创建语料快照。")
        return self.get(project_id, snapshot_id, "corpus_snapshot")

    def item(self, snapshot: dict, version_id: str):
        items = [i for i in snapshot["content"]["items"] if i["version_id"] == version_id]
        require(items, "EVIDENCE_MISMATCH", "文献不在选定语料快照中。")
        return items[0]

    def references(self, snapshot: dict, references: list[EvidenceReference]):
        resolved, rights, dependencies = [], [], []
        for reference in references:
            item = self.item(snapshot, reference.version_id)
            spans = []
            for evidence_id in reference.evidence_ids:
                span = self.store.get(evidence_id, "evidence")
                require(all(span[k] == item[k] for k in ("version_id", "asset_id", "parse_artifact_id", "sha256")),
                        "EVIDENCE_MISMATCH", "证据与语料快照不一致。")
                spans.append(span)
                rights.append(span["rights"])
                dependencies.append(evidence_id)
            observation = None
            if reference.observation_id:
                require(reference.observation_id == item["observation_id"], "EVIDENCE_MISMATCH", "摘要观测与语料快照不一致。")
                observation = self.store.get(reference.observation_id, "observation")
                rights.append(item["metadata_rights"])
                dependencies.append(reference.observation_id)
            require(spans or observation, "MISSING_EVIDENCE", "字段或论断需提供实际证据或题录观测。")
            # Match quotes across contiguous blocks, never across a missing range or a page boundary.
            from .review_evidence import ReviewEvidence
            texts = [s["text"] for s in ReviewEvidence.merged(spans)] + ([observation["fields"].get("abstract") or ""] if observation else [])
            require(not reference.quotation or any(reference.quotation in t for t in texts),
                    "QUOTATION_NOT_FOUND", "原文引语未出现在绑定证据中。")
            resolved.append({**reference.model_dump(), "asset_id": item["asset_id"] if spans else None,
                "parse_artifact_id": item["parse_artifact_id"] if spans else None,
                "sha256": item["sha256"] if spans else None, "pdf_pages": sorted({s["pdf_page"] for s in spans}),
                "reading_level": "fulltext" if spans else "abstract_only" if observation["fields"].get("abstract") else "metadata_only"})
        return resolved, rights, dependencies

    def save_card(self, project_id: str, snapshot_id: str, card: PaperCardInput, **revision):
        self.project(project_id, writing=True)
        snapshot = self.corpus(project_id, snapshot_id)
        item = self.item(snapshot, card.version_id)
        require(len({f.name for f in card.fields}) == len(card.fields), "INVALID_ARGUMENT", "文献卡字段名称重复。")
        if not revision.get("entity_id"):
            require(not any(c["content"]["version_id"] == card.version_id for c in self.heads(project_id, "paper_card")),
                    "REVISION_CONFLICT", "该文献已有阅读卡，请读取后显式修订。")
        fields, rights, dependencies = [], [], [snapshot["id"]]
        for field in card.fields:
            require(all(r.version_id == card.version_id for r in field.references), "EVIDENCE_MISMATCH", "文献卡不能绑定其他论文的字段证据。")
            require(not field.human_checked or revision.get("actor_role") == "user", "INVALID_ARGUMENT", "人工核查需明确提交用户角色。")
            require(field.value is None or field.source_type not in {"source_statement", "author_interpretation"}
                    or field.references, "MISSING_EVIDENCE", "原文事实或作者解释需绑定实际材料。")
            refs, field_rights, deps = self.references(snapshot, field.references)
            fields.append({**field.model_dump(), "state": "unknown" if field.value is None else field.state,
                           "references": refs})
            rights.extend(field_rights)
            dependencies.extend(deps)
        if not rights:
            rights = [item["fulltext_rights"] if item["reading_level"] == "fulltext" else item["metadata_rights"]]
        if card.sections:
            require(item["parse_artifact_id"], "FULLTEXT_NOT_PARSED", "章节页段需实际解析产物。")
            count = self.store.get(item["parse_artifact_id"], "parse")["page_count"]
            require(all(s.page_to <= count for s in card.sections), "INVALID_ARGUMENT", "章节页段超出 PDF。")
            require(revision.get("actor_role") == "user" or not any(s.verified_by_user for s in card.sections),
                    "INVALID_ARGUMENT", "Agent 不能代填人工章节核查。")
        if revision.get("entity_id"):
            previous = self.get(project_id, revision["entity_id"], "paper_card")
            require(previous["content"]["version_id"] == card.version_id, "EVIDENCE_MISMATCH", "不能改变文献卡的发表版本。")
        record = self.revision(project_id, "paper_card", {**card.model_dump(), "fields": fields,
            "snapshot_id": snapshot["id"], "reading_level": item["reading_level"]}, rights=rights,
            dependencies=dependencies, **revision)
        return self.present(record)

    def artifact(self, project_id: str, artifact_id: str | None, kind: str | None, offset: int, limit: int):
        self.project(project_id)
        require(kind is None or kind in ARTIFACT_KINDS, "INVALID_ARGUMENT", "不支持的综述记录类型。")
        if artifact_id:
            return {"artifact": self.present(self.get(project_id, artifact_id, kind))}
        records = self.heads(project_id, kind)
        return {"artifacts": [self.present(r) for r in records[offset:offset + limit]], "total": len(records),
                "next_offset": offset + limit if offset + limit < len(records) else None}

    def progress(self, project_id: str):
        project = self.project(project_id)
        cards = self.heads(project_id, "paper_card")
        snapshot = self.corpus(project_id, None) if project.get("review_snapshot_id") else None
        selected = snapshot["content"]["items"] if snapshot else []
        current_cards = [c for c in cards if snapshot and c["content"]["snapshot_id"] == snapshot["id"]]
        sections = self.heads(project_id, "review_section")
        checks = self.heads(project_id, "review_check")
        section_progress = []
        for section in sections:
            active_checks = [c for c in checks if c["content"]["section_revision_id"] == section["id"]
                             and self.present(c)["current_dependency_status"] == "unchanged"]
            queue = []
            for sentence in section["content"]["sentence_inventory"]:
                dispositions = {role: [a for c in active_checks if c["actor_role"] == role
                                      for a in c["content"]["sentence_assessments"] if a["sentence_id"] == sentence["sentence_id"]]
                                for role in ("agent", "user")}
                queue.append({"sentence_id": sentence["sentence_id"], "paragraph_id": sentence["paragraph_id"],
                    "model_disposition": dispositions["agent"][-1]["disposition"] if dispositions["agent"] else "unresolved",
                    "user_disposition": dispositions["user"][-1]["disposition"] if dispositions["user"] else "unresolved",
                    "mapped_claim_ids": sentence["mapped_claim_ids"], "risk_signals": sentence["risk_signals"]})
            section_progress.append({"section_id": section["entity_id"], "revision_id": section["id"],
                "dependency_status": self.present(section)["current_dependency_status"], "check_ids": [c["id"] for c in active_checks],
                "sentence_queue": queue, "semantic_support_certified": False})
        return {"project_id": project_id, "enabled": project.get("review_assistance_enabled", False),
            "snapshot_id": snapshot["id"] if snapshot else None,
            "artifact_counts": {k: len(self.heads(project_id, k)) for k in sorted(ARTIFACT_KINDS)},
            "materials": [{"version_id": i["version_id"], "reading_level": i["reading_level"], "quality": i["quality"]} for i in selected],
            "missing_cards": [i["version_id"] for i in selected if i["version_id"] not in {c["content"]["version_id"] for c in current_cards}],
            "fields": [{"card_id": c["entity_id"], "version_id": c["content"]["version_id"],
                        "unknown": sum(f["value"] is None for f in c["content"]["fields"]),
                        "extracted": sum(f["value"] is not None for f in c["content"]["fields"]),
                        "human_checked": sum(f["human_checked"] for f in c["content"]["fields"])} for c in current_cards],
            "returned_ranges": [r["content"] for r in self.heads(project_id, "reading_progress")],
            "sections": section_progress,
            "next_actions": (["enable_explicitly_before_enhanced_writes"] if not project.get("review_assistance_enabled", False) else [])
                + (["create_corpus_snapshot"] if not snapshot else [])
                + (["read_and_save_missing_paper_cards"] if len(current_cards) < len(selected) else [])
                + (["prepare_one_section"] if current_cards and not sections else [])
                + (["review_sentence_queue_and_core_claims"] if sections else []),
            "understanding_certified": False, "automatic_work_started": False}
