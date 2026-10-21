import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter, apply_configuration
from pypdf.errors import LimitReachedError, PdfReadError

from cnki_mcp.core import AppError
from cnki_mcp.pdf_validation import inspect_pdf
from cnki_mcp.pdf_worker import parse as parse_worker
from cnki_mcp.review_models import CorpusItemInput

from .conftest import make_duplicate_key_pdf, make_pdf


async def test_duplicate_dictionary_pdf_remains_immutable_and_requires_review(app, paper, rights):
    project = (await app.create_project("PDF producer compatibility"))["data"]["project_id"]
    version = app.library.ingest(project, paper, {"origin": "import", **rights.model_dump()}, "user_import")["version_id"]
    app.store.db.commit()
    path = app.settings.import_roots[0] / "duplicate.pdf"
    make_duplicate_key_pdf(path)
    original = path.read_bytes()
    registered = await app.import_fulltext(version, str(path), rights)
    assert registered["status"] == "succeeded", registered
    asset = registered["data"]
    assert asset["validation"] == "pdf_structural_review_required" and asset["pdf_structure_warning_count"] > 0
    assert asset["sha256"] == hashlib.sha256(original).hexdigest()
    submitted = await app.parse_fulltext(asset["id"])
    await app.jobs.tasks[submitted["job_id"]]
    job = (await app.get_job(submitted["job_id"]))["data"]
    assert job["state"] == "partially_succeeded", job
    assert job["result"]["quality"] == "structure_review_required"
    read = (await app.read_paper(version))["data"]
    assert read["reading_level"] == "fulltext" and read["needs_ocr_or_manual_review"]
    assert "Synthetic duplicate dictionary" in read["evidence"][0]["text"]
    assert path.read_bytes() == original


def test_duplicate_dictionary_does_not_allow_a_truncated_pdf(tmp_path):
    path = tmp_path / "truncated.pdf"
    make_duplicate_key_pdf(path)
    path.write_bytes(path.read_bytes().replace(b"%%EOF", b""))
    with pytest.raises((AppError, PdfReadError)):
        inspect_pdf(path, 200)


@pytest.mark.parametrize("opening_password", ["", "synthetic-required-password"])
async def test_default_reader_pdf_access_preserves_bytes_and_password_required_is_rejected(
        app, paper, rights, opening_password):
    project = (await app.create_project("Default PDF reader access"))["data"]["project_id"]
    version = app.library.ingest(project, paper, {"origin": "import", **rights.model_dump()}, "user_import")["version_id"]
    app.store.db.commit()
    source = app.settings.import_roots[0] / "source.pdf"
    encrypted = source.with_name("encrypted.pdf")
    make_pdf(source)
    writer = PdfWriter(clone_from=source)
    writer.encrypt(user_password=opening_password, owner_password="synthetic-owner-password", algorithm="RC4-128")
    writer.write(encrypted)
    original = encrypted.read_bytes()
    response = await app.import_fulltext(version, str(encrypted), rights)
    assert encrypted.read_bytes() == original
    if opening_password:
        assert response["error"]["code"] == "FILE_VALIDATION_FAILED" and response["error"]["requires_user_action"]
        assert not app.store.all("asset")
        return
    assert response["status"] == "succeeded", response
    asset = response["data"]
    assert asset["pdf_encryption"] == "default_reader_access"
    assert asset["sha256"] == hashlib.sha256(original).hexdigest()
    with pytest.raises(AppError):
        inspect_pdf(encrypted, 1)  # Default access does not bypass page limits.
    accepted = await app.parse_fulltext(asset["id"])
    await app.jobs.tasks[accepted["job_id"]]
    parsed = (await app.get_job(accepted["job_id"]))["data"]
    assert parsed["state"] == "succeeded", parsed
    artifact = app.store.get(parsed["result"]["parse_artifact_id"], "parse")
    assert artifact["pdf_encryption"] == "default_reader_access"
    read = (await app.read_paper(version))["data"]
    assert "Prediction improved" in read["evidence"][0]["text"]
    assert encrypted.read_bytes() == original


def make_stream_limit_pdf(path):
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path))
    for index in range(100):
        canvas.drawString(40, 750 - index * 4, "Repeated drawing operations in the guarded page.")
    canvas.showPage()
    canvas.drawString(40, 750, "Surviving evidence remains on the second physical PDF page.")
    canvas.save()


def test_page_stream_limit_is_not_raised_or_retried_and_other_pages_remain_readable(tmp_path):
    path, output = tmp_path / "guarded.pdf", tmp_path / "parsed.json"
    make_stream_limit_pdf(path)
    original = path.read_bytes()
    with apply_configuration(stream_parsing_work_maximum_count=150):
        with pytest.raises(LimitReachedError):
            parse_worker(path, output, 200)  # Older consumers cannot mistake partial output for success.
        parse_worker(path, output, 200, allow_partial_page_failures=True)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["extraction_failures"] == [{"pdf_page": 1, "code": "content_stream_token_limit"}]
    assert result["pages"][0]["text"] == "" and not result["scanned_pages"]
    assert "Surviving evidence" in result["pages"][1]["text"]
    assert result["pages"][1]["pdf_page"] == 2 and path.read_bytes() == original
    other_output = tmp_path / "other-limit.json"
    with apply_configuration(dictionary_maximum_element_count=1):
        with pytest.raises(LimitReachedError):
            parse_worker(path, other_output, 200, allow_partial_page_failures=True)
    assert not other_output.exists()  # Other parser safety failures still stop the document.


async def test_partial_page_extraction_gaps_survive_reading_snapshot_jobs_and_export(app, paper, rights, monkeypatch):
    project = (await app.create_project("Partial extraction"))["data"]["project_id"]
    imported = await app.import_records(project, [paper], rights=rights)
    version = imported["data"]["imported"][0]["version_id"]
    path = app.settings.import_roots[0] / "guarded.pdf"
    make_stream_limit_pdf(path)
    asset = (await app.import_fulltext(version, str(path), rights))["data"]

    class Finished:
        returncode = 0

        async def wait(self):
            return 0

    async def simulated_worker(*args, **kwargs):
        assert args[-1] == "--report-page-failures"
        with apply_configuration(stream_parsing_work_maximum_count=150):
            parse_worker(Path(args[3]), Path(args[4]), int(args[5]), allow_partial_page_failures=True)
        return Finished()

    monkeypatch.setattr("cnki_mcp.parsing.asyncio.create_subprocess_exec", simulated_worker)
    submitted = await app.parse_fulltext(asset["id"])
    await app.jobs.tasks[submitted["job_id"]]
    job = (await app.get_job(submitted["job_id"]))["data"]
    assert job["state"] == "partially_succeeded" and job["result"]["extraction_failed_pages"] == [1], job
    bad = (await app.read_paper(version, pages=[1]))["data"]
    assert not bad["evidence"] and bad["unavailable_pdf_pages"] == [1] and bad["needs_ocr_or_manual_review"]
    await app.set_review_assistance(project, True)
    snapshot = (await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))["data"]["id"]
    enhanced = (await app.read_paper(version, pages=[1], project_id=project, snapshot_id=snapshot))["data"]
    assert not enhanced["evidence"] and enhanced["unavailable_pdf_pages"] == [1]
    progress = (await app.get_review_progress(project))["data"]
    assert not progress["returned_ranges"] and progress["materials"][0]["unavailable_pdf_pages"] == [1]
    materials = await app.prepare_synthesis(project, snapshot, "Surviving")
    await app.jobs.tasks[materials["job_id"]]
    matrix = (await app.get_job(materials["job_id"]))["data"]["result"]["matrix"]
    assert matrix[0]["unavailable_pdf_pages"] == [1] and matrix[0]["evidence"]
    exported = (await app.export_project(project, include_review=True))["data"]
    package = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    frozen = next(a for a in package["review_artifacts"] if a["kind"] == "corpus_snapshot")
    assert frozen["content"]["value"]["items"][0]["extraction_failed_pages"] == [1]
