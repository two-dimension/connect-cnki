import hashlib

import pytest
from pypdf import PdfWriter
from pypdf.errors import PdfReadError

from cnki_mcp.core import AppError
from cnki_mcp.pdf_validation import inspect_pdf

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
