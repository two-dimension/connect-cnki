import json

from cnki_mcp.models import ImportRights
from cnki_mcp.review_models import (
    CardField,
    CorpusItemInput,
    EvidenceReference,
    PaperCardInput,
    ReviewPlanInput,
    SectionRange,
)

from .conftest import make_pdf
from .test_workflow import add_paper, parse_pdf


async def workspace(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    await app.set_review_assistance(project, True)
    plan = await app.save_review_plan(project, ReviewPlanInput(goal="Compare prediction", synonyms={"预测": ["prediction"]}))
    path = app.settings.import_roots[0] / "workspace.pdf"
    make_pdf(path)
    asset, parsed = await parse_pdf(app, version, path, rights)
    snapshot = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)], plan["data"]["id"])
    assert snapshot["status"] == "succeeded", snapshot
    return project, version, snapshot["data"]["id"], asset, parsed


async def test_opt_in_default_old_projects_and_base_compatibility(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    old = app.store.get(project)
    old.pop("review_assistance_enabled")
    app.store.put("project", old)
    other = (await app.create_project("other"))["data"]["project_id"]
    assert not (await app.get_review_progress(project))["data"]["enabled"]
    assert (await app.save_review_plan(project, ReviewPlanInput(goal="综述")))["error"]["code"] == "REVIEW_ASSISTANCE_DISABLED"
    assert (await app.prepare_review(project))["status"] == "succeeded"
    assert (await app.save_review(project, f"Unreviewed [{version}]", []))["status"] == "succeeded"
    await app.set_review_assistance(project, True)
    assert not (await app.get_review_progress(other))["data"]["enabled"]
    assert not app.store.all("paper_card")
    assert not app.store.all("parse")


async def test_card_snapshot_history_conflict_and_cross_project(app, paper, rights):
    project, version, snapshot, _, parsed = await workspace(app, paper, rights)
    read = (await app.read_paper(version, project_id=project, snapshot_id=snapshot))["data"]
    assert read["parse_artifact_id"] == parsed["parse_artifact_id"]
    ref = EvidenceReference(version_id=version, evidence_ids=read["evidence"][0]["evidence_ids"])
    card = PaperCardInput(version_id=version, fields=[CardField(name="results", value="12 percent under test conditions",
        references=[ref], state="extracted"), CardField(name="method")],
        sections=[SectionRange(name="results", page_from=1, page_to=1)])
    saved = (await app.save_paper_card(project, snapshot, card))["data"]
    changed = card.model_copy(deep=True)
    changed.fields[0].value = "A user correction, with original conditions"
    revised = await app.save_paper_card(project, snapshot, changed, saved["entity_id"], saved["id"], "user", "correct wording")
    assert revised["status"] == "succeeded"
    assert (await app.save_paper_card(project, snapshot, card, saved["entity_id"], saved["id"]))["error"]["code"] == "REVISION_CONFLICT"
    historical = (await app.get_review_artifact(project, saved["id"]))["data"]["artifact"]
    assert historical["content"]["value"]["fields"][0]["value"] == "12 percent under test conditions"
    assert (await app.read_paper(version, project_id=project, section_name="results"))["data"]["evidence"]
    other = (await app.create_project("other"))["data"]["project_id"]
    assert (await app.get_review_artifact(other, saved["id"]))["error"]["code"] == "PROJECT_MISMATCH"
    await app.set_review_assistance(project, False)
    assert (await app.get_review_artifact(project, revised["data"]["id"]))["status"] == "succeeded"
    assert (await app.save_paper_card(project, snapshot, card))["error"]["code"] == "REVIEW_ASSISTANCE_DISABLED"
    assert (await app.get_review_progress(project))["data"]["returned_ranges"]
    assert not (await app.get_review_progress(project))["data"]["understanding_certified"]


async def test_snapshot_does_not_switch_on_new_parse_or_metadata_change(app, paper, rights):
    project, version, snapshot, _, parsed = await workspace(app, paper, rights)
    path = app.settings.import_roots[0] / "new.pdf"
    make_pdf(path, "New opposing evidence, changed numerical conditions.")
    _, newparse = await parse_pdf(app, version, path, rights)
    record = app.store.get(version)
    record["title"] = "changed source title"
    app.store.put("version", record)
    read = (await app.read_paper(version, project_id=project))["data"]
    assert read["parse_artifact_id"] == parsed["parse_artifact_id"]
    assert (await app.read_paper(version, project_id=project, parse_artifact_id=newparse["parse_artifact_id"]))["error"]["code"] == "EVIDENCE_MISMATCH"
    frozen = (await app.get_review_artifact(project, snapshot))["data"]["artifact"]["content"]["value"]["items"][0]
    assert frozen["bibliography"]["title"] == paper.title
    new_span = next(e for e in app.store.all("evidence") if e["parse_artifact_id"] == newparse["parse_artifact_id"])
    bad = PaperCardInput(version_id=version, fields=[CardField(name="results", value="new",
        references=[EvidenceReference(version_id=version, evidence_ids=[new_span["id"]])])])
    assert (await app.save_paper_card(project, snapshot, bad))["error"]["code"] == "EVIDENCE_MISMATCH"


async def test_abstract_card_no_fabricated_pdf_and_no_automatic_jobs(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]
    item = snapshot["content"]["value"]["items"][0]
    card = PaperCardInput(version_id=version, fields=[CardField(name="question", value="Abstract-level extraction",
        references=[EvidenceReference(version_id=version, observation_id=item["observation_id"])])])
    saved = (await app.save_paper_card(project, snapshot["id"], card))["data"]["content"]["value"]
    assert saved["reading_level"] == "abstract_only"
    assert saved["fields"][0]["references"][0]["pdf_pages"] == []
    assert not app.store.all("job")
    assert (await app.read_paper(version, project_id=project))["error"]["code"] == "FULLTEXT_NOT_PARSED"


async def test_output_policy_in_cards_history_search_and_returned_progress(app, paper):
    rights = ImportRights(basis="parse only", allow_local_parse=True)
    project, version, snapshot, _, _ = await workspace(app, paper, rights)
    read = (await app.read_paper(version, project_id=project))["data"]
    assert read["evidence"][0]["text"]["withheld"]
    span = app.store.all("evidence")[0]
    card = PaperCardInput(version_id=version, fields=[CardField(name="results", value="PRIVATE_DERIVED_CARD",
        references=[EvidenceReference(version_id=version, evidence_ids=[span["id"]])])])
    saved = await app.save_paper_card(project, snapshot, card)
    assert saved["data"]["content"]["withheld"]
    for response in (await app.get_review_artifact(project, saved["data"]["id"]),
                     await app.search_evidence(project, "prediction"), await app.get_review_artifact(project, snapshot)):
        assert "PRIVATE_DERIVED_CARD" not in json.dumps(response)
        assert "PRIVATE_ABSTRACT" not in json.dumps(response)
    assert not (await app.get_review_progress(project))["data"]["returned_ranges"]


async def test_chinese_synonyms_context_and_budget_continuation(app, paper, rights):
    project, version, snapshot, _, _ = await workspace(app, paper, rights)
    result = (await app.search_evidence(project, "预测", snapshot_id=snapshot, context_blocks=1))["data"]
    assert result["results"] and result["semantic_search"] == "not_enabled"
    span = app.store.all("evidence")[0]
    app.store.put("evidence", {**span, "offset": 0, "text": "甲" * 1200})
    app.store.put("evidence", {**span, "id": "evidence_overlap", "offset": 1000, "text": "甲" * 200 + "乙" * 1000})
    app.settings.max_read_chars = 1300
    first = (await app.read_paper(version, project_id=project, snapshot_id=snapshot))["data"]
    assert first["truncated"] and first["next_evidence_offset"] == 1
    second = (await app.read_paper(version, project_id=project, evidence_offset=1))["data"]
    assert not second["truncated"]
    app.settings.max_read_chars = 6000
    merged = (await app.read_paper(version, project_id=project))["data"]
    assert merged["returned_chars"] == 2200 and len(merged["evidence"]) == 1
    assert len(merged["evidence"][0]["evidence_ids"]) == 2
    exact, words, grams = app.review_evidence.terms("中文研究条件", {})
    assert "研究" in grams and "中文研究条件" in exact and words


async def test_revision_history_survives_reopening_store(app, paper, rights):
    from cnki_mcp.app import Application
    project, version, snapshot, _, _ = await workspace(app, paper, rights)
    await app.read_paper(version, project_id=project)
    before = await app.get_review_progress(project)
    settings = app.settings
    await app.shutdown()
    reopened = Application(settings)
    try:
        after = await reopened.get_review_progress(project)
        assert after["data"] == before["data"]
        assert (await reopened.get_review_artifact(project, snapshot))["status"] == "succeeded"
    finally:
        await reopened.shutdown()
    # Fixture finalizer can safely shut down an already closed store.
    app.store.close = lambda: None
