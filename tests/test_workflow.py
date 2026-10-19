import json

from cnki_mcp.models import ClaimInput, ImportRights, ScreeningRules

from .conftest import make_pdf


async def add_paper(app, paper, rights):
    project = (await app.create_project("fixture question"))["data"]["project_id"]
    imported = await app.import_records(project, [paper], rights=rights)
    assert imported["status"] == "succeeded", imported
    return project, imported["data"]["imported"][0]["version_id"]


async def parse_pdf(app, version, path, rights):
    asset = await app.import_fulltext(version, str(path), rights)
    assert asset["status"] == "succeeded", asset
    parse = await app.parse_fulltext(asset["data"]["id"])
    await app.jobs.tasks[parse["job_id"]]
    job = await app.get_job(parse["job_id"])
    assert job["data"]["state"] == "succeeded", job
    return asset["data"], job["data"]["result"]


async def test_import_read_review_and_all_exports(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    path = app.settings.import_roots[0] / "fixture.pdf"
    make_pdf(path)
    asset, parsed = await parse_pdf(app, version, path, rights)
    read = await app.read_paper(version, [1])
    assert read["data"]["sha256"] == asset["sha256"]
    evidence = read["data"]["evidence"][0]
    assert evidence["pdf_page"] == 1 and evidence["printed_page"] is None
    assert "12 percent" in evidence["text"]
    assert (await app.search_evidence(project, "prediction"))["data"]["results"]
    matrix = await app.prepare_review(project)
    assert matrix["data"]["matrix"][0]["reading_level"] == "fulltext"
    claim = ClaimInput(text="A fixture reports improvement.", version_id=version, evidence_ids=[evidence["id"]],
                       quotation="12 percent")
    review = await app.save_review(project, f"{claim.text} [{version}]", [claim])
    assert review["data"]["validation"]["identity_valid"]
    assert review["data"]["validation"]["claims"][0]["quotation_verified"]
    assert review["data"]["validation"]["support_status"] == "not_certified"
    for format in ("ris", "bibtex", "csl-json", "text"):
        exported = await app.export_bibliography([version], format)
        assert exported["status"] == "succeeded", exported
        assert "Fixture power prediction" in exported["data"]["content"]
        assert "PRIVATE_ABSTRACT" not in exported["data"]["content"]
    assert (await app.export_bibliography([version], "text", "gbt-7714-2015"))["status"] == "succeeded"
    package = await app.export_project(project)
    assert package["status"] == "succeeded"
    assert json.loads(open(package["data"]["path"], encoding="utf-8").read())["reviews"]
    assert (await app.read_paper(version, [3]))["error"]["code"] == "INVALID_ARGUMENT"
    assert (await app.parse_fulltext(asset["id"], "ocr"))["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    # A new file/parse must not retarget previously selected reading or historical evidence.
    path2 = app.settings.import_roots[0] / "fixture2.pdf"
    make_pdf(path2, "Different updated text with different results for another snapshot.")
    asset2, parsed2 = await parse_pdf(app, version, path2, rights)
    assert parsed2["parse_artifact_id"] != parsed["parse_artifact_id"]
    reread = await app.read_paper(version)
    assert reread["data"]["parse_artifact_id"] == parsed["parse_artifact_id"]
    mismatch = await app.read_paper(version, asset_id=asset2["id"], parse_artifact_id=parsed["parse_artifact_id"])
    assert mismatch["error"]["code"] == "EVIDENCE_MISMATCH"


async def test_outbound_every_surface(app, paper):
    rights = ImportRights(basis="Local parsing only fixture", allow_local_parse=True)
    project, version = await add_paper(app, paper, rights)
    path = app.settings.import_roots[0] / "private.pdf"
    secret = "PRIVATE_BODY_fixture has confidential experimental values of 12 percent."
    make_pdf(path, secret)
    await parse_pdf(app, version, path, rights)
    read = await app.read_paper(version)
    assert read["data"]["evidence"][0]["text"]["withheld"]
    spans = app.store.all("evidence")
    claim = ClaimInput(text=secret, version_id=version, evidence_ids=[spans[0]["id"]])
    for result in (
        await app.get_paper(version), await app.list_papers(project), await app.prepare_review(project),
        await app.search_evidence(project, "PRIVATE_BODY"),
        await app.save_review(project, secret, [claim]),
    ):
        payload = json.dumps(result)
        assert "PRIVATE_ABSTRACT" not in payload and "PRIVATE_BODY" not in payload
    exported = await app.export_project(project)
    payload = open(exported["data"]["path"], encoding="utf-8").read()
    assert "PRIVATE_ABSTRACT" not in payload and "PRIVATE_BODY" not in payload


async def test_screening_unknown_recent_classic_and_rollback(app, paper, rights):
    project, version = await add_paper(app, paper, rights)
    policy = (await app.save_screening_policy(ScreeningRules(year_from=2025)))["data"]["policy_id"]
    decision = (await app.screen_papers([version], policy))["data"]["decisions"][0]
    assert decision["result"] == "excluded"
    override = await app.review_decision(decision["id"], "recommended", "Classic study")
    assert override["status"] == "succeeded"
    assert app.store.get(decision["id"], "decision")["result"] == "excluded"
    strict = (await app.save_screening_policy(ScreeningRules(mode="strict", missing_strategy="include")))["data"]["policy_id"]
    fulltext = (await app.screen_papers([version], strict, "fulltext"))["data"]["decisions"][0]
    assert fulltext["result"] == "pending_review" and fulltext["evidence_coverage"] == 0
    disabled = (await app.save_screening_policy(ScreeningRules(enabled=False, year_from=2025)))["data"]["policy_id"]
    assert (await app.screen_papers([version], disabled))["data"]["decisions"][0]["result"] == "recommended"
    conflict = await app.import_records(project, [paper.model_copy(update={"authors": ["Another Author"]})], rights=rights)
    candidate = conflict["data"]["imported"][0]["duplicate_ids"][0]
    merged = await app.resolve_duplicate(candidate, "same_work", "Confirmed related versions")
    merge_id = merged["data"]["merge_id"]
    assert len(app.store.papers(project)) == 2
    assert len({p["work_id"] for p in app.store.papers(project)}) == 1
    assert (await app.rollback_merge(merge_id, "Undo fixture"))["status"] == "succeeded"
    assert len({p["work_id"] for p in app.store.papers(project)}) == 2


async def test_paths_validation_and_claim_failures(app, paper, rights, tmp_path):
    project, version = await add_paper(app, paper, rights)
    outside = tmp_path / "outside.pdf"
    make_pdf(outside)
    assert (await app.import_fulltext(version, str(outside), rights))["error"]["code"] == "PATH_NOT_ALLOWED"
    fake = app.settings.import_roots[0] / "login.pdf"
    fake.write_text("<html>Please login SECRET</html>")
    assert (await app.import_fulltext(version, str(fake), rights))["error"]["code"] == "FILE_VALIDATION_FAILED"
    invalid = await app.save_review(project, "A false citation [version_fake]", [
        ClaimInput(text="A false citation", version_id="version_fake", evidence_ids=["evidence_fake"]),
    ])
    assert not invalid["data"]["validation"]["identity_valid"]
    assert invalid["data"]["validation"]["unknown_citations"] == ["version_fake"]
