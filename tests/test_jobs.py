import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from cnki_mcp.config import Institution, PolicyRecord
from cnki_mcp.core import AppError, Store
from cnki_mcp.jobs import Jobs


async def test_failed_download_preserves_unvalidated_file_without_registering_an_asset(app, paper, monkeypatch):
    profile = Institution(institution_id="fixture", display_names=["Fixture"], policy_records=[PolicyRecord(operation="fulltext_download",
        state="allowed", source="Synthetic fixture", verified_by="tests", resource_types=["*"],
        verified_at=datetime.now(UTC) - timedelta(days=1), expires_at=datetime.now(UTC) + timedelta(days=1))])
    app.settings.institutions.append(profile)
    project = (await app.create_project("Rejected download", "fixture"))["data"]["project_id"]
    rights = {"origin": "institution", "institution_id": "fixture", "resource_type": "journal"}
    version = app.library.ingest(project, paper, rights, "cnki_search")["version_id"]
    sid = app.store.put("session", {"institution_id": "fixture", "mode": "institution_network", "state": "ready"})["id"]
    app.store.db.commit()
    invalid_pdf = b"%PDF-1.4\nSynthetic invalid PDF\n%%EOF\n"

    @asynccontextmanager
    async def online(*args):
        yield object(), profile

    async def download(context, institution, session_id, record, target):
        target.write_bytes(invalid_pdf)

    monkeypatch.setattr(app, "online", online)
    monkeypatch.setattr(app.provider, "download", download)
    submitted = await app.acquire_fulltext(project, sid, version, "rejected-fixture")
    await app.jobs.tasks[submitted["job_id"]]
    result = (await app.get_job(submitted["job_id"]))["data"]
    assert result["state"] == "failed" and result["error"]["code"] == "FILE_VALIDATION_FAILED"
    assert not app.store.all("asset")
    rejected = app.store.root / "rejected_downloads"
    manifest = json.loads((rejected / f"{submitted['job_id']}.json").read_text(encoding="utf-8"))
    sha = hashlib.sha256(invalid_pdf).hexdigest()
    assert manifest["sha256"] == sha and manifest["diagnostic_code"] == "PdfReadError"
    assert manifest["state"] == "unvalidated_not_an_asset"
    assert (rejected / f"{sha}.unvalidated").read_bytes() == invalid_pdf


async def test_idempotency_cancel_and_restart(tmp_path):
    store = Store(tmp_path)
    jobs = Jobs(store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(job):
        started.set()
        await release.wait()
        return {"asset_id": "fixture"}

    jobs.handlers["fixture"] = handler
    first = jobs.submit("fixture", {"x": 1}, "same-key")
    repeat = jobs.submit("fixture", {"x": 1}, "same-key")
    assert repeat["id"] == first["id"]
    try:
        jobs.submit("fixture", {"x": 2}, "same-key")
        raise AssertionError("different request must not reuse key")
    except AppError as error:
        assert error.code == "IDEMPOTENCY_CONFLICT"
    await started.wait()
    assert (await jobs.cancel(first["id"]))["state"] == "cancelled"
    assert (await jobs.cancel(first["id"]))["state"] == "cancelled"
    assert jobs.resume(first["id"])["state"] == "cancelled"
    started.clear()
    second = jobs.submit("fixture", {"x": 3}, "restart-key")
    await started.wait()
    await jobs.shutdown()
    assert store.get(second["id"])["state"] == "waiting_for_user"
    store.close()
    reopened = Store(tmp_path)
    recovered = Jobs(reopened)

    async def restored(job):
        return {"restored": True}

    recovered.handlers["fixture"] = restored
    recovered.resume(second["id"])
    await recovered.tasks[second["id"]]
    assert recovered.present(reopened.get(second["id"]))["state"] == "succeeded"
    reopened.close()


async def test_download_resume_after_reauthentication_keeps_school_and_mode(tmp_path):
    store = Store(tmp_path)
    jobs = Jobs(store)
    with store.transaction():
        old = store.put("session", {"institution_id": "fixture", "mode": "institution_network", "state": "expired"})
        new = store.put("session", {"institution_id": "fixture", "mode": "institution_network", "state": "ready"})
        wrong = store.put("session", {"institution_id": "other", "mode": "institution_network", "state": "ready"})

    async def download(job):
        if job["arguments"]["session_id"] == old["id"]:
            raise AppError("SESSION_EXPIRED", "fixture expired", "re-authenticate")
        return {"asset_id": "fixture"}

    jobs.handlers["acquire_fulltext"] = download
    submitted = jobs.submit("acquire_fulltext", {"session_id": old["id"]}, "resume-key")
    await jobs.tasks[submitted["id"]]
    assert store.get(submitted["id"])["state"] == "waiting_for_user"
    try:
        jobs.resume(submitted["id"], wrong["id"])
        raise AssertionError("must not change institutions")
    except AppError as error:
        assert error.code == "SESSION_MISMATCH"
    jobs.resume(submitted["id"], new["id"])
    await jobs.tasks[submitted["id"]]
    assert store.get(submitted["id"])["state"] == "succeeded"
    assert jobs.submit("acquire_fulltext", {"session_id": old["id"]}, "resume-key")["id"] == submitted["id"]
    store.close()
