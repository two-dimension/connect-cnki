import asyncio
import json
from pathlib import Path

from cnki_mcp.review_models import CorpusItemInput, ReviewCheckInput, ReviewPlanInput, ReviewSectionInput

from .test_review_workspace import workspace
from .test_review_writing import written_section


async def test_disable_checkpoint_reenable_and_explicit_resume_no_base_cancellation(app, paper, rights, monkeypatch):
    project, version, _, _, _ = await workspace(app, paper, rights)
    second = (await app.import_records(project, [paper.model_copy(update={"title": "Other prediction", "doi": "10.1234/other"})], rights=rights))["data"]["imported"][0]["version_id"]
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version), CorpusItemInput(version_id=second)]))["data"]["id"]
    calls = []
    original = app.review_evidence.search
    def intercept(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(kwargs["version_ids"][0])
        if len(calls) == 1:
            app.review_workspace.set_enabled(project, False)
            app.review_workspace.set_enabled(project, True)
        return result
    monkeypatch.setattr(app.review_evidence, "search", intercept)
    release = asyncio.Event()
    async def base_handler(job):
        await release.wait()
        return {"completed": True}
    app.jobs.handlers["base_fixture"] = base_handler
    base = app.jobs.submit("base_fixture", {"project_id": project}, "shared-base")
    accepted = await app.prepare_synthesis(project, snapshot, "prediction")
    await app.jobs.tasks[accepted["job_id"]]
    paused = (await app.get_job(accepted["job_id"]))["data"]
    assert paused["state"] == "waiting_for_user" and paused["checkpoint"]["next_index"] == 1
    assert "rows" not in paused["checkpoint"] and calls == [version]
    assert not app.jobs.tasks[base["id"]].done()
    await app.set_review_assistance(project, False)
    assert (await app.resume_job(accepted["job_id"]))["error"]["code"] == "REVIEW_ASSISTANCE_DISABLED"
    await app.set_review_assistance(project, True)
    assert (await app.get_job(accepted["job_id"]))["data"]["state"] == "waiting_for_user"
    await app.resume_job(accepted["job_id"])
    await app.jobs.tasks[accepted["job_id"]]
    completed = (await app.get_job(accepted["job_id"]))["data"]
    assert completed["state"] == "succeeded" and len(completed["result"]["matrix"]) == 2
    assert calls == [version, second]
    release.set()
    await app.jobs.tasks[base["id"]]
    assert (await app.get_job(base["id"]))["data"]["state"] == "succeeded"


async def test_plan_revision_impact_check_expiration_and_protected_diff(app, paper, rights):
    project, _, _, _, section, _ = await written_section(app, paper, rights)
    check = (await app.save_review_check(project, section["id"], ReviewCheckInput()))["data"]
    plan = app.review_workspace.heads(project, "review_plan")[0]
    revised = await app.save_review_plan(project, ReviewPlanInput(goal="Changed research boundaries"),
        plan["entity_id"], plan["id"], reason="narrow the scope")
    impact = (await app.get_review_impact(project, [plan["entity_id"]], plan["id"], revised["data"]["id"]))["data"]
    affected = {r["revision_id"] for r in impact["affected"]}
    assert section["id"] in affected and check["id"] in affected
    assert "Changed research boundaries" in impact["diff"]
    assert (await app.get_review_artifact(project, check["id"]))["data"]["artifact"]["current_dependency_status"] == "outdated"
    assert not impact["old_snapshot_rewritten"]
    other = (await app.create_project("unrelated"))["data"]["project_id"]
    assert not (await app.get_review_impact(other))["data"]["affected"]


async def test_new_research_only_reports_coverage_and_old_snapshot_replays(app, paper, rights):
    project, version, snapshot, _, section, _ = await written_section(app, paper, rights)
    newer = (await app.import_records(project, [paper.model_copy(update={"title": "Opposing experiment", "doi": "10.1234/opposing"})], rights=rights))["data"]["imported"][0]["version_id"]
    impact = (await app.get_review_impact(project))["data"]
    assert impact["new_version_ids"] == [newer] and impact["coverage_review_needed"]
    assert section["id"] in impact["coverage_review_section_ids"]
    assert (await app.read_paper(version, project_id=project, snapshot_id=snapshot))["data"]["snapshot_id"] == snapshot
    assert (await app.get_review_artifact(project, section["id"]))["data"]["artifact"]["current_dependency_status"] == "unchanged"
    assert (await app.search_evidence(project, "Opposing", snapshot_id=snapshot, version_ids=[newer]))["error"]["code"] == "PROJECT_MISMATCH"


async def test_default_export_no_review_then_explicit_export_while_disabled(app, paper, rights):
    project, _, _, _, section, _ = await written_section(app, paper, rights)
    base = (await app.export_project(project))["data"]
    package = json.loads(Path(base["path"]).read_text(encoding="utf-8"))
    assert not {"matrix", "reviews", "review_artifacts", "document"}.intersection(package)
    await app.set_review_assistance(project, False)
    exported = await app.export_project(project, include_review=True, format="markdown", review_revision_ids=[section["id"]])
    assert exported["status"] == "succeeded", exported
    text = Path(exported["data"]["path"]).read_text(encoding="utf-8")
    assert "[1]" in text and "Fixture power prediction" in text and "## Prediction" in text
    manifest = json.loads(Path(exported["data"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["review_manifest"]["section_revision_ids"] == [section["id"]]
    assert manifest["artifact_manifest"]["quality_acceptance"] == "pending_human_topic_pilot"


async def test_export_renumbers_after_section_reorder_and_keeps_snapshots(app, paper, rights):
    project, version, _, _, section, _ = await written_section(app, paper, rights)
    second = (await app.import_records(project, [paper.model_copy(update={"title": "Second experiment", "doi": "10.1234/second"})], rights=rights))["data"]["imported"][0]["version_id"]
    snapshot2 = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version), CorpusItemInput(version_id=second)]))["data"]["id"]
    other = (await app.save_review(project, f"Background [{second}].", snapshot_id=snapshot2,
        section=ReviewSectionInput(title="Background", question="Where?")))["data"]
    first = (await app.export_project(project, True, review_revision_ids=[section["id"], other["id"]]))["data"]
    second_export = (await app.export_project(project, True, review_revision_ids=[other["id"], section["id"]]))["data"]
    a = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
    b = json.loads(Path(second_export["path"]).read_text(encoding="utf-8"))
    assert a["review_manifest"]["version_ids"] == [version, second]
    assert b["review_manifest"]["version_ids"] == [second, version]
    assert "[1]" in a["formatted_bibliography"].splitlines()[0]
    assert "Second experiment" in b["formatted_bibliography"].splitlines()[0]
    assert "Background [1]" in b["document"]["body"]


async def test_review_denial_applies_to_history_jobs_diff_and_all_export_files(app, paper, rights):
    project, version, snapshot, _, section, _ = await written_section(app, paper, rights)
    secret_body = "PRIVATE_REVIEW_NEW_BODY with changed values."
    revised = (await app.save_review(project, secret_body, snapshot_id=snapshot,
        section=ReviewSectionInput(title="Private", question="Why?"), entity_id=section["entity_id"],
        parent_revision_id=section["id"]))["data"]
    app.settings.client.allowed_content.remove("review")
    diff = await app.get_review_impact(project, base_revision_id=section["id"], target_revision_id=revised["id"])
    assert diff["data"]["diff"]["withheld"]
    artifact = await app.get_review_artifact(project, revised["id"])
    assert artifact["data"]["artifact"]["content"]["withheld"]
    for format in ("json", "markdown"):
        exported = await app.export_project(project, True, format=format)
        for key in ("path", "manifest_path"):
            text = Path(exported["data"][key]).read_text(encoding="utf-8")
            assert secret_body not in text
    read = (await app.read_paper(version, project_id=project))["data"]
    assert isinstance(read["evidence"][0]["text"], str)  # fulltext permission is independent


async def test_fulltext_denial_cannot_be_bypassed_by_review_quotes_or_job_cache(app, paper, rights):
    project, _, snapshot, synthesis, section, _ = await written_section(app, paper, rights)
    job = await app.prepare_synthesis(project, snapshot, "prediction")
    await app.jobs.tasks[job["job_id"]]
    app.settings.client.allowed_content.remove("fulltext")
    for result in (await app.get_review_artifact(project, synthesis["id"]),
                   await app.get_review_artifact(project, section["id"]), await app.get_job(job["job_id"])):
        assert "Prediction improved by 12 percent" not in json.dumps(result)
    exported = (await app.export_project(project, True))["data"]
    for key in ("path", "manifest_path"):
        assert "Prediction improved by 12 percent" not in Path(exported[key]).read_text(encoding="utf-8")


async def test_repeated_sentences_keep_distinct_ids_and_language_conditions_need_recheck(app, paper, rights):
    project, _, snapshot, _, _ = await workspace(app, paper, rights)
    body = "This holds in the laboratory. This holds in the laboratory."
    first = (await app.save_review(project, body, snapshot_id=snapshot,
        section=ReviewSectionInput(title="Repeated", question="Under which conditions?")))["data"]
    same = (await app.save_review(project, body, snapshot_id=snapshot,
        section=ReviewSectionInput(title="Repeated", question="Under which conditions?"),
        entity_id=first["entity_id"], parent_revision_id=first["id"]))["data"]
    old_ids = [s["sentence_id"] for s in first["content"]["value"]["sentence_inventory"]]
    new_ids = [s["sentence_id"] for s in same["content"]["value"]["sentence_inventory"]]
    assert len(set(new_ids)) == 2 and new_ids == old_ids
    changed = (await app.save_review(project, body.replace("laboratory", "field"), snapshot_id=snapshot,
        section=ReviewSectionInput(title="Repeated", question="Conditions", editing_pass="language"),
        entity_id=first["entity_id"], parent_revision_id=same["id"]))["data"]
    assert changed["content"]["value"]["validation"]["language_pass_requires_fact_recheck"]


async def test_shutdown_keeps_review_checkpoint_and_restarts_without_auto_resume(app, paper, rights, monkeypatch):
    from cnki_mcp.app import Application
    project, version, _, _, _ = await workspace(app, paper, rights)
    second = (await app.import_records(project, [paper.model_copy(update={"title": "Second item", "doi": "10.1234/restart"})], rights=rights))["data"]["imported"][0]["version_id"]
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version), CorpusItemInput(version_id=second)]))["data"]["id"]
    first_done = asyncio.Event()
    original = app.review_evidence.search
    def intercept(*args, **kwargs):
        result = original(*args, **kwargs)
        first_done.set()
        return result
    monkeypatch.setattr(app.review_evidence, "search", intercept)
    job = await app.prepare_synthesis(project, snapshot, "prediction")
    await first_done.wait()
    settings = app.settings
    await app.shutdown()
    app.store.close = lambda: None
    reopened = Application(settings)
    try:
        status = (await reopened.get_job(job["job_id"]))["data"]
        assert status["state"] == "waiting_for_user" and status["checkpoint"]["next_index"] == 1
        assert not reopened.jobs.tasks
        await reopened.resume_job(job["job_id"])
        await reopened.jobs.tasks[job["job_id"]]
        done = (await reopened.get_job(job["job_id"]))["data"]
        assert done["state"] == "succeeded"
        assert [r["version_id"] for r in done["result"]["matrix"]] == [version, second]
    finally:
        await reopened.shutdown()
