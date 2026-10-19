import json

from cnki_mcp.review_models import (
    ClaimAssessment,
    ClaimSourceInput,
    ComparisonRow,
    EvidenceReference,
    ParagraphInput,
    ReviewCheckInput,
    ReviewClaimInput,
    ReviewSectionInput,
    SentenceAssessment,
    SynthesisInput,
    SynthesisThemeInput,
)

from .test_review_workspace import workspace


async def written_section(app, paper, rights):
    project, version, snapshot, _, _ = await workspace(app, paper, rights)
    span = next(e for e in app.store.all("evidence") if e["version_id"] == version and e["pdf_page"] == 1)
    reference = EvidenceReference(version_id=version, evidence_ids=[span["id"]], quotation="12 percent")
    synthesis = SynthesisInput(subquestion="Compare prediction", claims=[ReviewClaimInput(
        text="Prediction improved by 12 percent under test conditions.", sources=[ClaimSourceInput(relation="supports", reference=reference)])],
        themes=[SynthesisThemeInput(title="Conditions", dimensions=["test conditions"], rows=[
            ComparisonRow(version_id=version, dimensions={"baseline": None}, comparable=None, references=[reference])])])
    result = await app.save_synthesis(project, snapshot, synthesis)
    assert result["status"] == "succeeded", result
    saved = result["data"]
    claim = saved["content"]["value"]["claims"][0]
    body = f"{claim['text']} [{version}]\n\nEvery study has solved this problem."
    section = ReviewSectionInput(title="Prediction", question="Which conditions?", synthesis_ids=[saved["id"]],
        paragraphs=[ParagraphInput(text=f"{claim['text']} [{version}]", claim_ids=[claim["claim_id"]]),
                    ParagraphInput(text="Every study has solved this problem.")])
    result = await app.save_review(project, body, snapshot_id=snapshot, section=section)
    assert result["status"] == "succeeded", result
    return project, version, snapshot, saved, result["data"], reference


async def test_sentence_inventory_includes_unsubmitted_facts_and_separate_reviews(app, paper, rights):
    project, _, _, synthesis, section, ref = await written_section(app, paper, rights)
    content = section["content"]["value"]
    inventory = content["sentence_inventory"]
    assert len(inventory) >= 2
    assert any("Every study" in s["text"] and s["sentence_id"] in content["validation"]["unmapped_sentence_ids"] for s in inventory)
    claim_id = synthesis["content"]["value"]["claims"][0]["claim_id"]
    model = ReviewCheckInput(claim_assessments=[ClaimAssessment(claim_id=claim_id, status="partially_supported",
        reason="Reported conditions do not justify universal generalization", references=[ref])])
    saved = (await app.save_review_check(project, section["id"], model))["data"]
    assert saved["content"]["value"]["opinion_origin"] == "caller_model"
    assert not saved["content"]["value"]["sentence_inventory_accounted"]
    user = ReviewCheckInput(review_type="human", issues=["Check overgeneralization against source"])
    assert (await app.save_review_check(project, section["id"], user))["error"]["code"] == "INVALID_ARGUMENT"
    human = (await app.save_review_check(project, section["id"], user, actor_role="user"))["data"]
    assert human["id"] != saved["id"]
    assert human["content"]["value"]["opinion_origin"] == "user_declared"
    assert human["content"]["value"]["support_status"] == "not_certified"


async def test_user_paragraphs_preserved_and_revision_checks_expire(app, paper, rights):
    project, _, snapshot, synthesis, section, ref = await written_section(app, paper, rights)
    content = section["content"]["value"]
    paragraphs = [ParagraphInput(paragraph_id=p["paragraph_id"], text=p["text"], claim_ids=p["claim_ids"]) for p in content["paragraphs"]]
    user_input = ReviewSectionInput(title="Prediction", question="Conditions", synthesis_ids=[synthesis["id"]], paragraphs=paragraphs)
    paragraphs[1].text = "A human correction requires verification."
    body = "\n\n".join(p.text for p in paragraphs)
    revised = (await app.save_review(project, body, snapshot_id=snapshot, section=user_input,
        entity_id=section["entity_id"], parent_revision_id=section["id"], actor_role="user"))["data"]
    old_check = (await app.save_review_check(project, revised["id"], ReviewCheckInput()))["data"]
    paragraphs[1].text = "Agent overwrites the user correction."
    user_input.paragraphs = paragraphs
    conflict = await app.save_review(project, "\n\n".join(p.text for p in paragraphs), snapshot_id=snapshot,
        section=user_input, entity_id=section["entity_id"], parent_revision_id=revised["id"])
    assert conflict["error"]["code"] == "USER_EDIT_CONFLICT"
    paragraphs[1].text = "A second human correction."
    new = await app.save_review(project, "\n\n".join(p.text for p in paragraphs), snapshot_id=snapshot,
        section=user_input, entity_id=section["entity_id"], parent_revision_id=revised["id"], actor_role="user")
    assert new["status"] == "succeeded", new
    assert (await app.get_review_artifact(project, old_check["id"]))["data"]["artifact"]["current_dependency_status"] == "outdated"
    assert (await app.get_review_artifact(project, section["id"]))["data"]["artifact"]["content"]["value"]["body"] == content["body"]


async def test_language_pass_changes_numbers_units_negation_and_requires_recheck(app, paper, rights):
    project, _, snapshot, synthesis, section, _ = await written_section(app, paper, rights)
    paragraphs = [ParagraphInput(paragraph_id=p["paragraph_id"], text=p["text"].replace("12 percent", "120 kg"),
                    claim_ids=p["claim_ids"]) for p in section["content"]["value"]["paragraphs"]]
    revised = await app.save_review(project, "\n\n".join(p.text for p in paragraphs), snapshot_id=snapshot,
        section=ReviewSectionInput(title="Prediction", question="Conditions", synthesis_ids=[synthesis["id"]],
            paragraphs=paragraphs, editing_pass="language"), entity_id=section["entity_id"], parent_revision_id=section["id"])
    assert revised["data"]["content"]["value"]["validation"]["language_pass_requires_fact_recheck"]


async def test_deterministic_invalid_citations_and_unbound_model_opinion(app, paper, rights):
    project, _, snapshot, synthesis, _, _ = await written_section(app, paper, rights)
    body = "This is unsupported [version_missing]."
    saved = await app.save_review(project, body, snapshot_id=snapshot,
        section=ReviewSectionInput(title="Invalid", question="Why?", synthesis_ids=[synthesis["id"]]))
    validation = saved["data"]["content"]["value"]["validation"]
    assert not validation["identity_valid"] and validation["unknown_citations"] == ["version_missing"]
    claim_id = synthesis["content"]["value"]["claims"][0]["claim_id"]
    bad = ReviewCheckInput(claim_assessments=[ClaimAssessment(claim_id=claim_id, status="fully_supported", reason="No evidence")])
    assert (await app.save_review_check(project, saved["data"]["id"], bad))["error"]["code"] == "MISSING_EVIDENCE"
    sentence = saved["data"]["content"]["value"]["sentence_inventory"][0]
    bad = ReviewCheckInput(sentence_assessments=[SentenceAssessment(sentence_id=sentence["sentence_id"], disposition="factual")])
    assert (await app.save_review_check(project, saved["data"]["id"], bad))["error"]["code"] == "MISSING_EVIDENCE"


async def test_synthesis_multiple_relations_gap_scope_and_stable_ids(app, paper, rights):
    project, version, snapshot, saved, _, ref = await written_section(app, paper, rights)
    old = saved["content"]["value"]["claims"][0]
    claim = ReviewClaimInput(claim_id=old["claim_id"], text="Qualified conclusion", sources=[
        ClaimSourceInput(source_id=old["sources"][0]["source_id"], relation="supports", reference=ref),
        ClaimSourceInput(relation="supports", reference=ref), ClaimSourceInput(relation="contradicts", reference=ref),
        ClaimSourceInput(relation="qualifies", reference=ref), ClaimSourceInput(relation="background", reference=ref)])
    result = await app.save_synthesis(project, snapshot, SynthesisInput(subquestion="Why?", claims=[claim]),
        saved["entity_id"], saved["id"])
    current = result["data"]["content"]["value"]["claims"][0]
    assert current["claim_id"] == old["claim_id"] and current["independent_support_groups"] == 1
    assert {s["relation"] for s in current["sources"]} == {"supports", "contradicts", "qualifies", "background"}
    badgap = SynthesisInput(subquestion="Gap", claims=[ReviewClaimInput(text="No research exists", claim_type="gap")])
    assert (await app.save_synthesis(project, snapshot, badgap))["error"]["code"] == "INVALID_ARGUMENT"
    # A stable ID from a different synthesis cannot be injected into a new one.
    assert (await app.save_synthesis(project, snapshot, SynthesisInput(subquestion="Why?", claims=[claim])))["error"]["code"] == "REVISION_CONFLICT"
    assert version == ref.version_id


async def test_enhanced_preparation_is_local_idempotent_and_reuses_cards(app, paper, rights):
    project, version, snapshot, _, _ = await workspace(app, paper, rights)
    first = await app.prepare_review(project, "prediction", [version], snapshot_id=snapshot)
    assert first.get("job_id"), first
    await app.jobs.tasks[first["job_id"]]
    job = (await app.get_job(first["job_id"]))["data"]
    assert job["state"] == "succeeded" and job["result"]["matrix"][0]["evidence"]
    again = await app.prepare_review(project, "prediction", [version], snapshot_id=snapshot)
    assert again["job_id"] == first["job_id"]
    synthesis = await app.prepare_synthesis(project, snapshot, "prediction")
    await app.jobs.tasks[synthesis["job_id"]]
    assert (await app.get_job(synthesis["job_id"]))["data"]["result"]["model_calls"] == 0
    assert len(app.store.all("job")) == 3  # one imported PDF parse and two local material jobs
    assert "WRITE-12" in json.dumps(job["result"])
