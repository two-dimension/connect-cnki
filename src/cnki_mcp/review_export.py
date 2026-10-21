from __future__ import annotations

import json
import re
from pathlib import Path

from .citations import export_manifest, render_review_async
from .core import digest, require, uid
from .policy import protected_all
from .review_workspace import ARTIFACT_KINDS


class ReviewExporter:
    def __init__(self, workspace, settings):
        self.workspace, self.settings, self.store = workspace, settings, workspace.store

    async def build(self, project_id, revision_ids=None, style="gbt-7714-2025"):
        sections = [self.workspace.get(project_id, r, "review_section") for r in revision_ids] if revision_ids is not None else self.workspace.heads(project_id, "review_section")
        require(len({s["entity_id"] for s in sections}) == len(sections), "INVALID_ARGUMENT", "一份正文不能同时采用同一章节的多个修订。")
        papers, known, requirements, bodies, issues = [], {}, [], [], []
        for section in sections:
            snapshot = self.workspace.corpus(project_id, section["content"]["snapshot_id"])
            items = {i["version_id"]: i for i in snapshot["content"]["items"]}
            body = section["content"]["body"]
            for version_id in re.findall(r"\[(version_[A-Za-z0-9_]+)\]", body):
                if version_id not in items:
                    issues.append({"section_id": section["id"], "code": "UNKNOWN_CITATION", "version_id": version_id})
                    continue
                paper = items[version_id]["bibliography"]
                if version_id in known:
                    require(digest(known[version_id]) == digest(paper), "BIBLIOGRAPHY_SNAPSHOT_CONFLICT", "不同章节对同一版本采用不同题录快照，请选择一致修订。")
                else:
                    known[version_id] = paper
                    papers.append(paper)
            requirements.extend(self.workspace.requirements(section))
            bodies.append(f"## {section['content']['title']}\n\n{body}")
            validation = section["content"]["validation"]
            for code, values in (("UNMAPPED_SENTENCE", validation["unmapped_sentence_ids"]),
                                 ("UNCITED_CLAIM_SOURCE", validation["uncited_claim_sources"])):
                issues.extend({"section_id": section["id"], "code": code, "id": value} for value in values)
        body = "\n\n".join(bodies)
        citation_ids = [v for v in re.findall(r"\[(version_[A-Za-z0-9_]+)\]", body) if v in known]
        style_path = self.settings.csl_styles.get(style, Path(__file__).parent / "styles" / f"{style}.csl")
        rendered = await render_review_async(papers, citation_ids, style_path, self.settings.render_timeout_seconds)
        labels = iter(rendered["citations"])
        formatted = re.sub(r"\[(version_[A-Za-z0-9_]+)\]", lambda m: next(labels) if m[1] in known
                           else f"[待核查：{m[1]}]", body)
        document = {"body": formatted, "internal_body": body, "section_revision_ids": [s["id"] for s in sections]}
        document = protected_all(document, requirements)
        manifest = export_manifest(papers, "text", style, style_path)
        manifest.update(format="review", citation_order=citation_ids, entry_ids=rendered["entry_ids"],
                        section_revision_ids=[s["id"] for s in sections], support_status="not_certified")
        artifacts = [self.workspace.present(r) for kind in sorted(ARTIFACT_KINDS)
                     for r in self.workspace.records(project_id, kind)]
        pending, visited, cards = [s["id"] for s in sections], set(), {}
        while pending:
            record_id = pending.pop()
            if record_id in visited:
                continue
            visited.add(record_id)
            record = self.store.get(record_id)
            if record.get("kind") == "paper_card":
                cards[record_id] = record
            pending.extend(record.get("dependencies", []))
        if not sections:
            cards = {c["id"]: c for c in self.workspace.heads(project_id, "paper_card")}
        matrix = [{"version_id": c["content"]["version_id"], "card_revision_id": c["id"],
                   "snapshot_id": c["content"]["snapshot_id"], "reading_level": c["content"]["reading_level"],
                   "analysis": self.workspace.protect_content(c, c["content"])} for c in cards.values()]
        return {"document": document, "formatted_bibliography": rendered["text"], "review_manifest": manifest,
                "paper_matrix": matrix, "review_artifacts": artifacts, "review_issues": issues,
                "quality_acceptance": "pending_human_topic_pilot"}

    def write(self, package, format):
        clean = self.workspace.gate.output(package)
        export_id = uid("project")
        output = self.store.root / "exports" / f"{export_id}.{'md' if format == 'markdown' else 'json'}"
        if format == "json":
            content = json.dumps(clean, ensure_ascii=False, indent=2)
        else:
            document = clean.get("document", {})
            body = document.get("body", "") if not document.get("withheld") else "[正文按内容策略屏蔽]"
            content = "# 文献项目导出\n\n" + body
            if clean.get("formatted_bibliography"):
                content += "\n\n## 参考文献\n\n" + clean["formatted_bibliography"]
            content += "\n\n## 核查问题与运行记录\n\n```json\n" + json.dumps(
                {k: v for k, v in clean.items() if k not in {"document", "formatted_bibliography", "review_artifacts"}},
                ensure_ascii=False, indent=2) + "\n```\n"
        output.write_text(content, encoding="utf-8")
        # JSON companion preserves matrices/history and makes Markdown replay independently inspectable.
        manifest_path = output.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps({"export_id": export_id, "format": format, "content_policy_applied": True,
            "review_manifest": clean.get("review_manifest"), "artifact_manifest": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(output), "manifest_path": str(manifest_path), "content_policy_applied": True,
                "include_review": "document" in clean}
