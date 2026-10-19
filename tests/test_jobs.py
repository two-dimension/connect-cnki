import asyncio

from cnki_mcp.core import AppError, Store
from cnki_mcp.jobs import Jobs


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
    store.db.close()
    reopened = Store(tmp_path)
    recovered = Jobs(reopened)

    async def restored(job):
        return {"restored": True}

    recovered.handlers["fixture"] = restored
    recovered.resume(second["id"])
    await recovered.tasks[second["id"]]
    assert recovered.present(reopened.get(second["id"]))["state"] == "succeeded"
    reopened.db.close()
