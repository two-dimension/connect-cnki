"""Regression triggers from IMP-01 through IMP-10, using isolated synthetic data."""
import asyncio
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from anyio import CancelScope

from cnki_mcp import parsing_identity
from cnki_mcp.config import ClientProfile, Institution, PolicyRecord
from cnki_mcp.core import AppError, Store, digest
from cnki_mcp.jobs import Jobs
from cnki_mcp.models import DimensionRating, ImportRights, ScreeningRules
from cnki_mcp.processes import run_process
from cnki_mcp.review_models import CorpusItemInput

from .conftest import make_pdf
from .test_workflow import add_paper, parse_pdf


async def synthetic_evidence(app, paper, rights, text=None):
    project, version = await add_paper(app, paper, rights)
    access = {"origin": "import", **rights.model_dump()}
    asset = app.store.put("asset", {"version_id": version, "sha256": "synthetic-hash", "rights": access})
    artifact = app.store.put("parse", {"version_id": version, "asset_id": asset["id"], "sha256": asset["sha256"],
        "page_count": 1, "quality": "text_extracted_unreviewed", "scanned_pages": [], "complex_pages": []})
    text = text or "".join(str(i % 10) * 1000 for i in range(11))
    spans = [app.store.put("evidence", {"version_id": version, "asset_id": asset["id"],
        "parse_artifact_id": artifact["id"], "sha256": asset["sha256"], "pdf_page": 1,
        "offset": start, "text": text[start:start + 1200], "rights": access}) for start in range(0, len(text), 1000)]
    app.store.db.commit()
    return project, version, artifact, spans


async def test_rating_uses_all_evidence_rights_across_recipients_exports_and_history(app, paper):
    open_rights = ImportRights(basis="synthetic metadata", allow_local_parse=True, allowed_recipients=["local", "openai"])
    project, version, _, spans = await synthetic_evidence(app, paper, open_rights)
    restrictive = {"origin": "import", "allowed_recipients": ["local"], "allow_local_parse": True}
    # Metadata and one evidence block permit the external recipient. Another asset does not.
    restricted_asset = app.store.put("asset", {"version_id": version, "rights": restrictive})
    restricted = app.store.put("evidence", {**spans[1], "asset_id": restricted_asset["id"]})
    policy = (await app.save_screening_policy(ScreeningRules()))["data"]["policy_id"]
    secret = "PRIVATE_FULLTEXT_DERIVATION"
    rating = DimensionRating(dimension="data_split", score=2, reason=secret,
                             evidence_ids=[spans[0]["id"], restricted["id"]])
    decision = (await app.screen_papers([version], policy, "fulltext", {version: [rating]}))["data"]["decisions"][0]
    assert secret in json.dumps(decision)
    await app.review_decision(decision["id"], "recommended", secret + "_override")
    app.settings.client = ClientProfile(location="external", provider="openai", allowed_content=["metadata", "abstract", "fulltext", "review"])
    app.store.db.commit()
    for format in ("json", "markdown"):
        exported = (await app.export_project(project, format=format))["data"]
        for key in ("path", "manifest_path"):
            payload = Path(exported[key]).read_text(encoding="utf-8")
            assert secret not in payload and '"withheld": true' in payload
    result = await app.screen_papers([version], policy, "fulltext", {version: [rating]})
    assert secret not in json.dumps(result)
    # Changing the current asset licence must affect an old decision, too.
    app.store.put("asset", {**restricted_asset, "rights": {**restrictive, "allowed_recipients": ["openai"]}})
    app.store.put("evidence", {**restricted, "rights": {**restrictive, "allowed_recipients": ["openai"]}})
    historical = app.gate.output(app.screening.present(app.store.get(decision["id"])))
    assert secret in json.dumps(historical)
    app.store.db.execute("DELETE FROM records WHERE id=?", (restricted["id"],))
    missing = app.gate.output(app.screening.present(app.store.get(decision["id"])))
    assert missing["dependency_status"] == "missing" and secret not in json.dumps(missing)


async def test_each_rating_keeps_its_own_rights_and_current_source_licence(app, paper, rights):
    _, version, _, spans = await synthetic_evidence(app, paper, rights)
    policies = [PolicyRecord(operation=operation, state="allowed", source="Synthetic licence", verified_by="tests",
        verified_at=datetime.now(UTC) - timedelta(days=1), expires_at=datetime.now(UTC) + timedelta(days=1),
        resource_types=["journal"], providers=["openai"]) for operation in ("local_parse_index", "external_model_use")]
    app.settings.institutions.append(Institution(institution_id="fixture", display_names=["Fixture"], policy_records=policies))
    institution_rights = {"origin": "institution", "institution_id": "fixture", "resource_type": "journal"}
    source_asset = app.store.put("asset", {"version_id": version, "rights": institution_rights})
    source_span = app.store.put("evidence", {**spans[0], "asset_id": source_asset["id"], "rights": institution_rights})
    policy = (await app.save_screening_policy(ScreeningRules()))["data"]["policy_id"]
    ratings = [DimensionRating(dimension="data_split", score=2, reason="SOURCE_REASON", evidence_ids=[source_span["id"]]),
               DimensionRating(dimension="baselines", score=2, reason="LOCAL_ONLY_REASON", evidence_ids=[spans[1]["id"]])]
    decision = (await app.screen_papers([version], policy, "fulltext", {version: ratings}))["data"]["decisions"][0]
    app.settings.client = ClientProfile(location="external", provider="openai", allowed_content=["metadata", "fulltext", "review"])
    visible = app.gate.output(app.screening.present(app.store.get(decision["id"])))
    assert visible["dimensions"][0]["reason"] == "SOURCE_REASON" and visible["dimensions"][2]["withheld"]
    policies[1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    revoked = app.gate.output(app.screening.present(app.store.get(decision["id"])))
    assert revoked["dimensions"][0]["withheld"] and "SOURCE_REASON" not in json.dumps(revoked)


async def test_long_page_base_continuation_is_complete_pinned_and_budgeted(app, paper, rights):
    _, version, artifact, spans = await synthetic_evidence(app, paper, rights)
    app.settings.max_read_chars = 2400
    arguments, ids, page_text, offsets = {"version_id": version}, set(), {}, []
    while arguments:
        first = (await app.read_paper(**arguments))["data"]
        repeat = (await app.read_paper(**arguments))["data"]
        assert first == repeat and first["returned_chars"] <= 2400
        assert first["parse_artifact_id"] == artifact["id"]
        for block in first["evidence"]:
            ids.update(block["evidence_ids"])
            for i, char in enumerate(block["text"], block["offset"]):
                assert i not in page_text or page_text[i] == char
                page_text[i] = char
        arguments = first["continuation"]
        if arguments:
            offsets.append(arguments["evidence_offset"])
    assert ids == {s["id"] for s in spans} and len(page_text) == 11000
    assert offsets == sorted(set(offsets))
    assert not app.store.all("reading_progress") and not app.store.all("corpus_snapshot")
    app.settings.max_read_chars = 100
    tiny = (await app.read_paper(version))["data"]
    assert tiny["budget_too_small"] and tiny["required_chars"] == 1200
    assert tiny["continuation"] is None and tiny["next_evidence_offset"] is None
    assert (await app.read_paper(version, evidence_offset=100))["error"]["code"] == "INVALID_ARGUMENT"


async def test_context_reduces_to_hit_and_reports_budget_and_policy_separately(app, paper, rights):
    text = "a" * 1000 + "needle" + "b" * 1994
    project, version, _, _ = await synthetic_evidence(app, paper, rights, text)
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]["id"]
    app.settings.max_read_chars = 1200
    found = (await app.search_evidence(project, "needle", context_blocks=1))["data"]
    assert found["results"] and found["returned_chars"] == 1200 and found["context_truncated"]
    assert "needle" in found["results"][0]["text"]
    tiny = app.review_evidence.search(project, "needle", snapshot_id=snapshot, max_chars=100)
    assert not tiny["results"] and tiny["version_status"][version] == "budget_insufficient"
    assert tiny["budget_insufficient"][0]["read_arguments"]["parse_artifact_id"]
    assert app.review_evidence.search(project, "absent")["version_status"][version] == "no_keyword_hit"
    progress_before = len(app.store.all("reading_progress"))
    app.settings.client.allowed_content = ["metadata"]
    withheld = (await app.search_evidence(project, "needle"))["data"]
    assert withheld["version_status"][version] == "policy_withheld" and withheld["returned_chars"] == 0
    assert len(app.store.all("reading_progress")) == progress_before


async def test_material_preparation_shares_budget_in_both_orders(app, paper, rights):
    versions, project = [], None
    for index in range(3):
        current, version, _, _ = await synthetic_evidence(app, paper.model_copy(update={
            "title": f"Synthetic fairness {index}", "doi": f"10.1234/fairness{index}"}), rights,
            "needle " + "a" * 2993)
        project = project or current
        app.store.link(project, version)
        versions.append(version)
    app.store.db.commit()
    app.settings.max_read_chars = 3600
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=v) for v in versions]))["data"]["id"]
    for order in (versions, versions[::-1]):
        submitted = await app.prepare_review(project, "needle", order, snapshot)
        await app.jobs.tasks[submitted["job_id"]]
        result = (await app.get_job(submitted["job_id"]))["data"]["result"]
        assert all(row["evidence_status"] == "returned" and row["evidence"] for row in result["matrix"])
        assert sum(len(e["text"]) for row in result["matrix"] for e in row["evidence"]) == 3600
        assert result["evidence_truncated"]


async def test_tight_material_budget_covers_every_paper_with_precise_excerpts_and_progress(app, paper, rights):
    versions, project = [], None
    for index in range(8):
        current, version, _, _ = await synthetic_evidence(app, paper.model_copy(update={
            "title": f"Synthetic tight budget {index}", "doi": f"10.1234/tight{index}"}), rights,
            "a" * 1100 + "needle" + "b" * 1894)
        project = project or current
        app.store.link(project, version)
        versions.append(version)
    app.store.db.commit()
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=v) for v in versions]))["data"]["id"]
    app.settings.max_read_chars = 6000
    for order in (versions, versions[::-1]):
        submitted = await app.prepare_review(project, "needle", order, snapshot)
        await app.jobs.tasks[submitted["job_id"]]
        before = {r["id"] for r in app.store.all("reading_progress")}
        result = (await app.get_job(submitted["job_id"]))["data"]["result"]
        assert not result["uncovered_version_ids"] and result["evidence_truncated"]
        returned = [e for row in result["matrix"] for e in row["evidence"]]
        assert len(returned) == 8 and sum(len(e["text"]) for e in returned) <= 6000
        assert all(row["hit_truncated"] and row["evidence_status"] == "returned" for row in result["matrix"])
        assert all("needle" in e["text"] and e["excerpt"] for e in returned)
        progress = [r for r in app.store.all("reading_progress") if r["id"] not in before]
        ranges = [span for record in progress for span in record["content"].get("ranges", [])]
        assert sum(r["length"] for r in ranges) == sum(len(e["text"]) for e in returned)
        assert {(r["version_id"], r["offset"], r["length"]) for r in ranges} == {
            (e["version_id"], e["offset"], len(e["text"])) for e in returned}


async def test_snapshot_reports_sparse_observation_and_preserves_explicit_rights(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    first_observation = app.store.find("observation", version_id=version)[0]["id"]
    await app.set_review_assistance(project, True)
    old = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]["id"]
    sparse = paper.model_copy(update={"abstract": None, "volume": None, "pages": None})
    imported = (await app.import_records(project, [sparse], rights=ImportRights(basis="no external grant")))["data"]["imported"][0]
    result = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)])
    latest = result["data"]["content"]["value"]["items"][0]
    assert result["warnings"] and set(latest["field_regressions"]) >= {"abstract", "volume", "pages"}
    assert latest["observation_id"] == imported["observation_id"] and latest["bibliography"]["abstract"]["withheld"]
    explicit = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version, observation_id=first_observation)])
    assert explicit["data"]["content"]["value"]["items"][0]["bibliography"]["abstract"] == paper.abstract
    frozen = (await app.get_review_artifact(project, old))["data"]["artifact"]["content"]["value"]["items"][0]
    assert frozen["bibliography"]["pages"] == paper.pages


async def test_partial_observation_cannot_relabel_an_inherited_abstract(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    await app.set_review_assistance(project, True)
    original = app.store.find("observation", version_id=version)[0]
    partial = app.store.put("observation", {"version_id": version, "fields": {"title": paper.title},
        "rights": {"origin": "import", "allowed_recipients": ["openai"]}, "source": "partial_fixture"})
    app.store.db.commit()
    app.settings.client = ClientProfile(location="external", provider="openai", allowed_content=["metadata", "abstract"])
    result = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)])
    item = result["data"]["content"]["value"]["items"][0]
    assert result["warnings"] and item["observation_id"] == partial["id"]
    assert item["bibliography"]["abstract"] is None and "PRIVATE_ABSTRACT" not in json.dumps(result)
    assert item["field_sources"]["volume"] == {"record_id": version, "kind": "version"}
    frozen = app.store.get(result["data"]["id"], "corpus_snapshot")["content"]["items"][0]
    assert frozen["field_sources"]["volume"]["rights"] == original["rights"]
    # An explicit full observation uses its own restriction, even after a permissive partial observation.
    explicit = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version, observation_id=original["id"])])
    assert explicit["data"]["content"]["value"]["items"][0]["bibliography"]["abstract"]["withheld"]


async def test_conflicting_abstract_observations_are_explicit_and_historical(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    original = app.store.find("observation", version_id=version)[0]["id"]
    await app.set_review_assistance(project, True)
    first = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]["id"]
    changed = paper.model_copy(update={"abstract": "A conflicting but explicitly selected source statement."})
    imported = (await app.import_records(project, [changed], rights=rights))["data"]["imported"][0]
    result = await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)])
    item = result["data"]["content"]["value"]["items"][0]
    assert result["warnings"] and "abstract" in item["field_conflicts"]
    assert item["observation_id"] == imported["observation_id"] and item["bibliography"]["abstract"] == changed.abstract
    assert item["alternative_observation_ids"] == [original]
    old = (await app.get_review_artifact(project, first))["data"]["artifact"]["content"]["value"]["items"][0]
    assert old["bibliography"]["abstract"] == paper.abstract


async def test_import_budget_counts_dedup_and_rolls_back_mixed_and_conflicting_batches(app, paper, rights):
    project = (await app.create_project("one slot", budget=1))["data"]["project_id"]
    assert (await app.import_records(project, [paper, paper], rights=rights))["status"] == "succeeded"
    assert app.store.paper_count(project) == 1 and len(app.store.all("observation")) == 2
    assert (await app.import_records(project, [paper], rights=rights))["status"] == "succeeded"
    before = {k: app.store.all(k) for k in ("version", "work", "observation", "duplicate")}
    conflict = paper.model_copy(update={"authors": ["Another author"]})
    for records in ([paper, conflict], [paper.model_copy(update={"doi": "10.1234/new", "title": "New"})]):
        result = await app.import_records(project, records, rights=rights)
        assert result["error"]["code"] == "BUDGET_EXCEEDED"
        assert before == {k: app.store.all(k) for k in before}
    other = (await app.create_project("other", budget=1))["data"]["project_id"]
    assert (await app.import_records(other, [paper], rights=rights))["status"] == "succeeded"
    assert app.store.paper_count(other) == 1 and len(app.store.all("version")) == 1


async def test_pipeline_identity_invalidates_old_success_but_not_pinned_evidence(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    path = app.settings.import_roots[0] / "pipeline.pdf"
    make_pdf(path)
    asset, parsed = await parse_pdf(app, version, path, rights)
    old_read = (await app.read_paper(version))["data"]
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]["id"]
    old_job = next(j for j in app.store.all("job") if j["arguments"].get("asset_id") == asset["id"])
    arguments = {**old_job["arguments"], "pipeline_version": "previous-installed-pipeline"}
    key = digest(["parse", asset["id"], arguments["parser_profile"], arguments["parser_version"],
                  arguments["pipeline_version"], arguments["config_digest"]])
    app.store.put("job", {**old_job, "arguments": arguments, "idempotency_key": key,
                          "fingerprint": digest({"operation": "parse_fulltext", "arguments": arguments})})
    artifact = app.store.get(parsed["parse_artifact_id"])
    app.store.put("parse", {**artifact, "pipeline_version": arguments["pipeline_version"]})
    app.store.db.commit()
    submitted = await app.parse_fulltext(asset["id"])
    assert submitted["job_id"] != old_job["id"]
    assert (await app.parse_fulltext(asset["id"]))["job_id"] == submitted["job_id"]
    await app.jobs.tasks[submitted["job_id"]]
    new = (await app.get_job(submitted["job_id"]))["data"]["result"]
    assert new["pipeline_version"] == parsing_identity.PIPELINE_VERSION
    assert new["parse_artifact_id"] != artifact["id"]
    assert (await app.read_paper(version))["data"]["parse_artifact_id"] == old_read["parse_artifact_id"]
    assert (await app.read_paper(version, project_id=project, snapshot_id=snapshot))["data"]["parse_artifact_id"] == artifact["id"]


async def test_queue_limit_cancel_failure_release_and_restart(tmp_path):
    store = Store(tmp_path / "queue")
    jobs = Jobs(store, {"parse_fulltext": 2})
    started, release = asyncio.Event(), asyncio.Event()
    active, peak, calls = 0, 0, []

    async def handler(job):
        nonlocal active, peak
        calls.append(job["arguments"]["index"])
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            if job["arguments"]["index"] == 0:
                raise RuntimeError("PRIVATE_JOB_SECRET")
            return {"done": True}
        finally:
            active -= 1

    jobs.handlers["parse_fulltext"] = handler
    records = [jobs.submit("parse_fulltext", {"index": i}, str(i)) for i in range(8)]
    await asyncio.wait_for(started.wait(), 2)
    assert sum(store.get(r["id"])["state"] == "running" for r in records) == 2
    assert store.get(records[-1]["id"])["state"] == "queued"
    await jobs.cancel(records[-1]["id"])
    pending = list(jobs.tasks.values())
    release.set()
    await asyncio.gather(*pending)
    assert peak == 2 and 7 not in calls and calls == list(range(7))
    assert store.get(records[0]["id"])["state"] == "failed"
    assert all(store.get(r["id"])["state"] == "succeeded" for r in records[1:-1])
    release.clear()
    waiting = [jobs.submit("parse_fulltext", {"index": i}, str(i)) for i in range(8, 12)]
    await asyncio.sleep(0)
    await jobs.shutdown()
    assert all(store.get(r["id"])["state"] == "waiting_for_user" for r in waiting)
    store.close()
    reopened = Store(tmp_path / "queue")
    assert reopened.get(records[1]["id"])["state"] == "succeeded"
    assert "PRIVATE_JOB_SECRET" not in str([dict(r) for r in reopened.db.execute("SELECT * FROM audit")])
    reopened.close()


@pytest.mark.parametrize(("failure", "expected"), [(PermissionError, "FILE_ACCESS_DENIED"),
    (TypeError, "INTERNAL_ERROR"), (KeyError, "INTERNAL_ERROR"), (ModuleNotFoundError, "DEPENDENCY_UNAVAILABLE")])
async def test_error_classification_and_diagnostics_never_echo_exception(app, paper, rights, monkeypatch, failure, expected):
    project = (await app.create_project("diagnostic fixture"))["data"]["project_id"]

    def broken(*args, **kwargs):
        raise failure("PRIVATE_CREDENTIAL_URL_BODY")

    monkeypatch.setattr(app.library, "ingest", broken)
    result = await app.import_records(project, [paper], rights=rights)
    assert result["error"]["code"] == expected
    row = dict(app.store.db.execute("SELECT * FROM audit WHERE request_id=?", (result["request_id"],)).fetchone())
    assert row["diagnostic_code"] == failure.__name__ and row["stage"] == "tool"
    assert "PRIVATE_CREDENTIAL_URL_BODY" not in json.dumps(result) + str(row)


async def test_malformed_import_is_an_input_error(app):
    project = (await app.create_project("malformed"))["data"]["project_id"]
    path = app.settings.import_roots[0] / "bad.json"
    path.write_text('[{"not_a_title": "PRIVATE_INVALID_RECORD"}]', encoding="utf-8")
    response = await app.import_records(project, file_path=str(path))
    assert response["error"]["code"] == "INVALID_ARGUMENT"
    assert "PRIVATE_INVALID_RECORD" not in json.dumps(response)


async def test_subprocess_timeout_cancel_reap_and_event_loop_responsiveness(monkeypatch):
    import cnki_mcp.processes as processes

    original = asyncio.create_subprocess_exec
    children = []

    async def capture(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", capture)
    args = [sys.executable, "-c", "import time; time.sleep(30)"]
    task = asyncio.create_task(run_process(args, timeout=30))
    async with asyncio.timeout(3):
        while not children:
            await asyncio.sleep(0.01)
    # A separate await is serviceable while the controlled slow process runs.
    assert await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.5) == "responsive"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None
    with pytest.raises(AppError) as timed_out:
        await run_process(args, timeout=0.05, timeout_code="RENDERER_TIMEOUT")
    assert timed_out.value.code == "RENDERER_TIMEOUT" and children[-1].returncode is not None


@pytest.mark.parametrize("operation", ["inspect", "render"])
async def test_slow_import_or_export_allows_status_queries_and_cleans_on_cancel(app, paper, rights, monkeypatch, operation):
    project, version = await add_paper(app, paper, rights)
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        return await run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30)

    if operation == "inspect":
        monkeypatch.setattr("cnki_mcp.parsing.run_process", slow)
        file = app.settings.import_roots[0] / "slow.pdf"
        make_pdf(file)
        work = app.import_fulltext(version, str(file), rights)
    else:
        monkeypatch.setattr("cnki_mcp.citations.run_process", slow)
        work = app.export_bibliography([version], "text")
    task = asyncio.create_task(work)
    await asyncio.wait_for(started.wait(), 3)
    status = await asyncio.wait_for(app.list_projects(), 0.5)
    assert status["data"]["projects"][0]["id"] == project
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app.store.all("asset") and not app.store.all("export")
    assert not list((app.store.root / "tmp").iterdir())


async def test_missing_renderer_is_actionable_without_echoing_data(app, paper, rights, monkeypatch):
    _, version = await add_paper(app, paper, rights)
    monkeypatch.setattr("cnki_mcp.citations.shutil.which", lambda _: None)
    response = await app.export_bibliography([version], "text")
    assert response["error"]["code"] == "RENDERER_UNAVAILABLE" and response["error"]["user_action"]
    assert "PRIVATE_ABSTRACT" not in json.dumps(response)


@pytest.mark.parametrize("operation", ["inspect", "render"])
async def test_import_export_timeout_is_distinct_and_leaves_no_incomplete_asset(app, paper, rights, monkeypatch, operation):
    _, version = await add_paper(app, paper, rights)

    async def timeout(*args, **kwargs):
        return await run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                 timeout=.05, timeout_code=kwargs["timeout_code"])

    if operation == "inspect":
        monkeypatch.setattr("cnki_mcp.parsing.run_process", timeout)
        file = app.settings.import_roots[0] / "timeout.pdf"
        make_pdf(file)
        response = await app.import_fulltext(version, str(file), rights)
        code = "PDF_INSPECTION_TIMEOUT"
    else:
        monkeypatch.setattr("cnki_mcp.citations.run_process", timeout)
        response = await app.export_bibliography([version], "text")
        code = "RENDERER_TIMEOUT"
    assert response["error"]["code"] == code and response["error"]["user_action"]
    assert not app.store.all("asset") and not app.store.all("export")
    assert not list((app.store.root / "tmp").iterdir())


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_parse_process_is_reaped_after_cancel_or_timeout(app, paper, rights, monkeypatch, finish):
    _, version = await add_paper(app, paper, rights)
    file = app.settings.import_roots[0] / "parse-wait.pdf"
    make_pdf(file)
    asset = (await app.import_fulltext(version, str(file), rights))["data"]
    original = asyncio.create_subprocess_exec
    started, children = asyncio.Event(), []

    async def controlled(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr("cnki_mcp.parsing.asyncio.create_subprocess_exec", controlled)
    app.settings.parse_timeout_seconds = 1
    submitted = await app.parse_fulltext(asset["id"])
    await asyncio.wait_for(started.wait(), 3)
    if finish == "cancel":
        result = (await app.cancel_job(submitted["job_id"]))["data"]
        assert result["state"] == "cancelled"
    else:
        await app.jobs.tasks[submitted["job_id"]]
        result = (await app.get_job(submitted["job_id"]))["data"]
        assert result["error"]["code"] == "PARSING_TIMEOUT"
    assert all(child.returncode is not None for child in children)
    assert not app.store.all("parse") and not app.store.all("evidence")
    assert not list((app.store.root / "tmp").iterdir())


def test_inspection_permission_error_is_not_misreported_as_corrupt_pdf(monkeypatch, tmp_path):
    from cnki_mcp.pdf_inspector import inspect

    def denied(*args):
        raise PermissionError("PRIVATE_INSPECTION_PATH")

    monkeypatch.setattr("cnki_mcp.pdf_inspector.inspect_pdf", denied)
    result = inspect(tmp_path / "fixture.pdf", 200)
    assert result["error"]["code"] == "FILE_ACCESS_DENIED"
    assert result["error"]["diagnostic_code"] == "PermissionError"
    assert "PRIVATE_INSPECTION_PATH" not in json.dumps(result)


async def test_pdf_worker_reports_only_a_sanitized_failure_category(tmp_path):
    source, destination = tmp_path / "invalid.pdf", tmp_path / "result.json"
    source.write_bytes(b"%PDF-1.4\nPRIVATE_PDF_CREDENTIAL\n%%EOF\n")
    child = await asyncio.create_subprocess_exec(sys.executable, "-m", "cnki_mcp.pdf_worker", str(source),
        str(destination), "200", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    stdout, _ = await child.communicate()
    result = json.loads(destination.read_text(encoding="utf-8"))
    assert child.returncode == 2 and not stdout
    assert result["error"]["stage"] == "pdf_extraction" and result["error"]["diagnostic_code"] == "PdfReadError"
    assert "PRIVATE_PDF_CREDENTIAL" not in json.dumps(result)


async def test_protocol_cancel_scope_during_process_creation_reaps_child(monkeypatch):
    import cnki_mcp.processes as processes

    original = asyncio.create_subprocess_exec
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children, scopes = [], []

    async def delayed_spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        started.set()
        await release.wait()
        return child

    async def work():
        with CancelScope() as scope:
            scopes.append(scope)
            try:
                await run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30)
            finally:
                finished.set()

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), 3)
        scopes[0].cancel()
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        await task
        assert children[0].returncode is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


async def test_protocol_cancel_waits_for_file_copy_before_removing_staging_file(app, paper, rights, monkeypatch):
    _, version = await add_paper(app, paper, rights)
    source = app.settings.import_roots[0] / "copy-cancel.pdf"
    make_pdf(source)
    loop = asyncio.get_running_loop()
    started, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
    scopes = []

    def slow_copy(source_file, destination):
        with Path(destination).open("wb") as target:
            target.write(b"%PDF-1.4\n")
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            target.write(b"synthetic staged copy\n%%EOF\n")
        return destination

    async def work():
        with CancelScope() as scope:
            scopes.append(scope)
            try:
                await app.import_fulltext(version, str(source), rights)
            finally:
                finished.set()

    monkeypatch.setattr("cnki_mcp.parsing.shutil.copyfile", slow_copy)
    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), 3)
        scopes[0].cancel()
        await asyncio.sleep(.02)
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(task, 3)
        assert not list((app.store.root / "tmp").iterdir()) and not app.store.all("asset")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
