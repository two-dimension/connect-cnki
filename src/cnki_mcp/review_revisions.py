from __future__ import annotations

import difflib
import json

from .core import require


class ReviewRevisions:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store

    def impact(self, project_id, changed_ids=None, base_revision_id=None, target_revision_id=None):
        project = self.workspace.project(project_id)
        roots = set()
        for record_id in changed_ids or []:
            row = self.store.db.execute("SELECT kind FROM records WHERE id=?", (record_id,)).fetchone()
            if row:
                kind = row[0]
                if kind in {"version", "asset", "parse", "evidence", "observation"}:
                    record = self.store.get(record_id)
                    version_id = record_id if kind == "version" else record["version_id"]
                    require(version_id in {p["id"] for p in self.store.papers(project_id)}, "PROJECT_MISMATCH", "变更材料不属于项目。")
                    roots.add(record_id)
                else:
                    record = self.workspace.get(project_id, record_id)
                    roots.add(record["id"])
                    roots.update(r["id"] for r in self.workspace.records(project_id, record["kind"])
                                 if r["entity_id"] == record["entity_id"])
            else:
                matches = [r for r in self.workspace.records(project_id, "synthesis")
                           if any(c["claim_id"] == record_id for c in r["content"]["claims"])]
                require(matches, "NOT_FOUND", "未找到项目中的稳定论断标识。")
                roots.update(r["id"] for r in matches)
        events = self.workspace.records(project_id, "review_invalidation")
        if not changed_ids:
            roots.update(e["source_id"] for e in events)
        affected, pending = set(), list(roots)
        while pending:
            source = pending.pop()
            for row in self.store.db.execute("SELECT dependent_id FROM review_dependencies WHERE project_id=? AND source_id=?",
                                             (project_id, source)):
                if row[0] not in affected:
                    affected.add(row[0])
                    pending.append(row[0])
        heads = {r["id"] for r in self.workspace.heads(project_id)}
        effects = []
        for record_id in sorted(affected):
            record = self.store.get(record_id)
            effects.append({"revision_id": record_id, "entity_id": record["entity_id"], "kind": record["kind"],
                "current_revision": record_id in heads, "reason": "declared_dependency_changed",
                "next_action": "recheck_dependencies_then_submit_local_revision",
                "current_dependency_status": self.workspace.present(record)["current_dependency_status"]})
        snapshot = self.workspace.corpus(project_id, None) if project.get("review_snapshot_id") else None
        items = snapshot["content"]["items"] if snapshot else []
        papers = {p["id"]: p for p in self.store.papers(project_id)}
        added = sorted(papers.keys() - {i["version_id"] for i in items})
        alternatives = [{"version_id": i["version_id"], "selected_parse_id": i["parse_artifact_id"],
                         "alternative_parse_ids": [p["id"] for p in self.store.all("parse")
                                                   if p["version_id"] == i["version_id"] and p["id"] != i["parse_artifact_id"]]}
                        for i in items]
        relation_changes = [i["version_id"] for i in items if papers[i["version_id"]]["work_id"] != i["work_id"]]
        result = {"project_id": project_id, "affected": effects, "new_version_ids": added,
            "alternative_parses": [a for a in alternatives if a["alternative_parse_ids"]],
            "work_relation_changed_version_ids": relation_changes,
            "coverage_review_needed": bool(added or relation_changes),
            "coverage_review_section_ids": sorted(r["id"] for r in self.workspace.heads(project_id, "review_section")) if added or relation_changes else [],
            "recommendations": (["review_new_studies_for_support_and_counterevidence_before_selecting_a_new_snapshot"] if added else [])
                + (["recheck_independent_support_groups_using_updated_work_relations"] if relation_changes else []),
            "old_snapshot_rewritten": False, "automatic_actions": False}
        require(bool(base_revision_id) == bool(target_revision_id), "INVALID_ARGUMENT", "版本差异需同时指定基准和目标。")
        if base_revision_id:
            base = self.workspace.get(project_id, base_revision_id)
            target = self.workspace.get(project_id, target_revision_id)
            require(base["entity_id"] == target["entity_id"], "REVISION_CONFLICT", "只比较同一成果的修订。")
            left = base["content"].get("body") or json.dumps(base["content"], ensure_ascii=False, sort_keys=True, indent=2)
            right = target["content"].get("body") or json.dumps(target["content"], ensure_ascii=False, sort_keys=True, indent=2)
            delta = "\n".join(difflib.unified_diff(left.splitlines(), right.splitlines(), fromfile=base["id"], tofile=target["id"], lineterm=""))
            for record in (base, target):
                if record["rights"]:
                    delta = self.workspace.protect_content(record, delta)
            result["diff"] = delta
        return result
