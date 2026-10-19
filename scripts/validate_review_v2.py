"""Local engineering regression on a consistent COPY of the eight writing-study papers.

No network, model calls, scientific quality scoring, or writes to the source library.
Full artifacts stay in ignored data/tmp; the optional report contains only identifiers,
hashes, counts and engineering assertions.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path

from cnki_mcp import __version__
from cnki_mcp.app import Application
from cnki_mcp.config import Settings
from cnki_mcp.core import digest, now
from cnki_mcp.review_models import (
    CardField,
    ClaimAssessment,
    ClaimSourceInput,
    ComparisonRow,
    CorpusItemInput,
    EvidenceReference,
    PaperCardInput,
    ParagraphInput,
    ReviewCheckInput,
    ReviewClaimInput,
    ReviewPlanInput,
    ReviewSectionInput,
    SectionRange,
    SynthesisInput,
    SynthesisThemeInput,
)

STUDY_SOURCES = {"CJFQ:GLSJ202610002", "CJFQ:GLSJ202610008", "CJFQ:WLHX202611014", "CJFQ:WLHX202611021",
                 "CJFQ:JSJX202609004", "CJFQ:JSJX202609007", "CAPJ:HXXB20261014002", "CAPJ:HXXB20261013003"}


def ok(response):
    if response["status"] != "succeeded":
        raise RuntimeError(response.get("error", {}).get("code", "REGRESSION_FAILED"))
    return response["data"]


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def validate(config, project_id):
    started = time.perf_counter()
    settings = Settings.load(config)
    source_root = settings.data_dir.resolve()
    clone = source_root / "tmp" / f"review-v2-regression-{uuid.uuid4().hex}"
    assert clone.resolve().is_relative_to((source_root / "tmp").resolve())
    clone.mkdir(parents=True)
    with sqlite3.connect((source_root / "library.sqlite3").as_uri() + "?mode=ro", uri=True) as source:
        source_schema = source.execute("PRAGMA user_version").fetchone()[0]
        with sqlite3.connect(clone / "library.sqlite3") as destination:
            source.backup(destination)
    app = Application(settings.model_copy(update={"data_dir": clone}))
    hashes, card_models, cards = [], {}, []
    try:
        selected = [p for p in app.store.papers(project_id) if p.get("source_id") in STUDY_SOURCES]
        assert len(selected) == 8, "Expected all eight writing-study papers"
        selected.sort(key=lambda p: p["source_id"])
        inputs = []
        for paper in selected:
            artifact = next(p for p in app.store.all("parse") if p["version_id"] == paper["id"])
            asset = app.store.get(artifact["asset_id"], "asset")
            source_asset = source_root / asset["relative_path"]
            target = clone / asset["relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            assert checksum(source_asset) == asset["sha256"]
            shutil.copyfile(source_asset, target)
            assert checksum(target) == asset["sha256"]
            manifest = source_root / "parses" / f"{artifact['id']}.json"
            if manifest.exists():
                shutil.copyfile(manifest, clone / "parses" / manifest.name)
            hashes.append({"source_id": paper["source_id"], "version_id": paper["id"], "sha256": asset["sha256"],
                           "parse_artifact_id": artifact["id"], "quality": artifact["quality"]})
            inputs.append(CorpusItemInput(version_id=paper["id"], parse_artifact_id=artifact["id"]))
        assert not ok(await app.get_review_progress(project_id))["enabled"]
        ok(await app.set_review_assistance(project_id, True))
        plan = ok(await app.save_review_plan(project_id, ReviewPlanInput(goal="跨学科写作材料的工程回归，非同课题综述验收",
            subquestions=["定位、修订和导出能否重放？"], scope="仅既有八篇写作观察样本", synonyms={"方法": ["研究", "结果"]})))
        snapshot = ok(await app.create_corpus_snapshot(project_id, inputs, plan["id"]))
        snapshot_id = snapshot["id"]
        references, snippets = {}, {}
        for item in inputs:
            span = min((e for e in app.store.all("evidence") if e["parse_artifact_id"] == item.parse_artifact_id and e["text"].strip()),
                       key=lambda e: (e["pdf_page"], e["offset"]))
            read = ok(await app.read_paper(item.version_id, pages=[span["pdf_page"]], project_id=project_id, snapshot_id=snapshot_id))
            assert any(isinstance(e["text"], str) for e in read["evidence"]), "Source output permission required"
            snippet = span["text"][:80]
            references[item.version_id] = EvidenceReference(version_id=item.version_id, evidence_ids=[span["id"]], quotation=snippet)
            snippets[item.version_id] = snippet
            card = PaperCardInput(version_id=item.version_id, fields=[
                CardField(name="定位片段", value=snippet, state="needs_review", references=[references[item.version_id]]),
                CardField(name="方法核查"), CardField(name="条件核查")],
                sections=[SectionRange(name="工程定位页", page_from=span["pdf_page"], page_to=span["pdf_page"])])
            card_models[item.version_id] = card
            cards.append(ok(await app.save_paper_card(project_id, snapshot_id, card)))
        materials = ok(await app.prepare_synthesis(project_id, snapshot_id, "研究 方法 结果", plan_id=plan["id"]))
        await app.jobs.tasks[materials["id"]]
        material_job = ok(await app.get_job(materials["id"]))
        assert material_job["state"] == "succeeded" and len(material_job["result"]["matrix"]) == 8
        synthesis = ok(await app.save_synthesis(project_id, snapshot_id, SynthesisInput(
            subquestion="工程定位记录", plan_id=plan["id"], card_ids=[c["id"] for c in cards],
            search_scope="现有八篇跨学科样本，不推断研究共识", themes=[SynthesisThemeInput(title="解析与定位状态",
                classification_reason="只比较工程材料状态", dimensions=["quality"], rows=[ComparisonRow(
                    version_id=h["version_id"], dimensions={"quality": h["quality"]}, comparable=False,
                    comparison_limits="学科与问题不同，不能据此比较科学结论", references=[references[h["version_id"]]]) for h in hashes])],
            claims=[*[ReviewClaimInput(text=snippets[i.version_id], scope="仅原文定位片段，语义尚未核查",
                sources=[ClaimSourceInput(relation="background", reference=references[i.version_id])]) for i in inputs],
                ReviewClaimInput(text="这些跨学科材料用于工程回归，不能据此推断同一课题的科学共识。", claim_type="synthesis",
                    scope="既有八篇材料的工程用途", sources=[ClaimSourceInput(relation="background", reference=references[i.version_id]) for i in inputs]) ])))
        claim_rows = synthesis["content"]["value"]["claims"]
        paragraphs = [ParagraphInput(text=f"工程定位片段：“{snippets[i.version_id]}” [{i.version_id}]", claim_ids=[claim["claim_id"]])
                      for i, claim in zip(inputs, claim_rows[:8], strict=True)]
        paragraphs.append(ParagraphInput(text=claim_rows[-1]["text"] + " " + "".join(f"[{i.version_id}]" for i in inputs),
                                         claim_ids=[claim_rows[-1]["claim_id"]]))
        section_model = ReviewSectionInput(title="工程回归片段集（非学科综述）", question="材料是否可重放？", plan_id=plan["id"],
            synthesis_ids=[synthesis["id"]], card_ids=[c["id"] for c in cards], paragraphs=paragraphs)
        body = "\n\n".join(p.text for p in paragraphs)
        section = ok(await app.save_review(project_id, body, snapshot_id=snapshot_id, section=section_model))
        check = ok(await app.save_review_check(project_id, section["id"], ReviewCheckInput(claim_assessments=[
            ClaimAssessment(claim_id=c["claim_id"], status="not_assessed", reason="仅工程回归，未进行语义人工验收") for c in claim_rows])))
        paragraphs = [ParagraphInput(paragraph_id=p["paragraph_id"], text=p["text"], claim_ids=p["claim_ids"])
                      for p in section["content"]["value"]["paragraphs"]]
        human_text = "此段为用户角色编辑保留测试；本轮未进行同课题综述质量验收。"
        paragraphs.append(ParagraphInput(text=human_text))
        section_model.paragraphs = paragraphs
        human_revision = ok(await app.save_review(project_id, "\n\n".join(p.text for p in paragraphs), snapshot_id=snapshot_id,
            section=section_model, entity_id=section["entity_id"], parent_revision_id=section["id"], actor_role="user", reason="engineering role test"))
        paragraphs = [ParagraphInput(paragraph_id=p["paragraph_id"], text=p["text"], claim_ids=p["claim_ids"])
                      for p in human_revision["content"]["value"]["paragraphs"]]
        paragraphs[0].text = "修订定位说明：" + paragraphs[0].text
        section_model.paragraphs = paragraphs
        final = ok(await app.save_review(project_id, "\n\n".join(p.text for p in paragraphs), snapshot_id=snapshot_id,
            section=section_model, entity_id=section["entity_id"], parent_revision_id=human_revision["id"], reason="localized engineering revision"))
        assert final["content"]["value"]["paragraphs"][-1]["text"] == human_text
        assert final["content"]["value"]["paragraphs"][-1]["authored_by"] == "user"
        first_card = cards[0]
        card = card_models[inputs[0].version_id].model_copy(deep=True)
        card.pending_questions.append("工程回归：此修订要求重新核查依赖")
        ok(await app.save_paper_card(project_id, snapshot_id, card, first_card["entity_id"], first_card["id"], reason="regression correction"))
        impact = ok(await app.get_review_impact(project_id, [first_card["entity_id"]]))
        assert final["id"] in {r["revision_id"] for r in impact["affected"]}
        assert ok(await app.get_review_artifact(project_id, check["id"]))["artifact"]["current_dependency_status"] == "outdated"
        historical = ok(await app.get_review_artifact(project_id, section["id"]))["artifact"]
        assert historical["content"]["value"]["body"] == body
        ok(await app.set_review_assistance(project_id, False))
        exports = [ok(await app.export_project(project_id, True, format=f, review_revision_ids=[final["id"]])) for f in ("json", "markdown")]
        exported = json.loads(Path(exports[0]["path"]).read_text(encoding="utf-8"))
        assert len(exported["review_manifest"]["version_ids"]) == 8
        assert len(exported["paper_matrix"]) == 8
        assert {c["card_revision_id"] for c in exported["paper_matrix"]} == {c["id"] for c in cards}
        assert exported["review_manifest"]["support_status"] == "not_certified"
        assert not ok(await app.get_review_progress(project_id))["enabled"]
        schema = app.store.db.execute("PRAGMA user_version").fetchone()[0]
        await app.shutdown()
        app = Application(settings.model_copy(update={"data_dir": clone}))
        assert ok(await app.get_review_artifact(project_id, section["id"]))["artifact"]["content"]["value"]["body"] == body
        assert not app.jobs.tasks and not ok(await app.get_review_progress(project_id))["enabled"]
        return {"at": now(), "service_version": __version__, "scope": "isolated_copy_engineering_regression_only", "source_database_access": "SQLite mode=ro backup API",
            "source_schema": source_schema, "test_schema": schema, "papers": hashes,
            "parse_quality_counts": dict(Counter(h["quality"] for h in hashes)), "paper_cards": 8,
            "claims": len(claim_rows), "multi_source_claim_count": sum(len(c["sources"]) > 1 for c in claim_rows),
            "material_rows": len(material_job["result"]["matrix"]),
            "material_uncovered_count": len(material_job["result"]["uncovered_version_ids"]),
            "human_edit_preserved": True, "reviewer_role_test": "caller_declared_role_not_independent_human_acceptance",
            "local_revision_impact_detected": True, "historical_body_digest": digest(body), "restart_replay_passed": True,
            "matrix_uses_frozen_card_revisions": True,
            "disabled_export_formats": ["json", "markdown"], "unified_citation_count": 8,
            "model_calls": 0, "network_calls": 0, "semantic_quality_acceptance": "not_performed",
            "clone_relative_path": str(clone.relative_to(source_root)), "elapsed_ms": round((time.perf_counter()-started)*1000, 3)}
    finally:
        await app.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--project-id", default="project_006fccc809ed4dc0bbf30e8663fc7345")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(asyncio.run(validate(args.config, args.project_id)), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)
