from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .core import AppError, Store, digest, now, require


class Jobs:
    def __init__(self, store: Store):
        self.store = store
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping = False
        self.handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {}

    def submit(self, operation: str, arguments: dict, idempotency_key: str) -> dict:
        fingerprint = digest({"operation": operation, "arguments": arguments})
        for job in self.store.all("job"):
            if job["idempotency_key"] == idempotency_key:
                require(job["fingerprint"] == fingerprint, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求。")
                return self.present(job)
        job = self.store.put("job", {
            "operation": operation, "arguments": arguments, "idempotency_key": idempotency_key,
            "fingerprint": fingerprint, "state": "queued", "progress": 0,
            "result": None, "error": None, "created_at": now(), "checkpoint": None,
        })
        self.store.db.commit()
        self.schedule(job)
        return self.present(job)

    def schedule(self, job: dict):
        self.tasks[job["id"]] = asyncio.create_task(self.run(job["id"]))

    async def run(self, job_id: str):
        job = self.store.get(job_id, "job")
        job.update(state="running", progress=0.05, error=None)
        self.store.put("job", job)
        self.store.db.commit()
        try:
            result = await self.handlers[job["operation"]](job)
            current = self.store.get(job_id, "job")
            if current["state"] != "cancelled":
                job.update(state="succeeded", progress=1, result=result, checkpoint="completed")
                self.store.put("job", job)
        except asyncio.CancelledError:
            job.update(state="waiting_for_user" if self.stopping else "cancelled",
                       checkpoint="interrupted" if self.stopping else "cancelled")
            self.store.put("job", job)
        except AppError as error:
            job.update(state="waiting_for_user" if error.action else "failed",
                       error={"code": error.code, "message": error.message, "user_action": error.action,
                              "retryable": error.retryable, "requires_user_action": bool(error.action)})
            self.store.put("job", job)
        except Exception:
            job.update(state="failed", error={"code": "INTERNAL_ERROR", "message": "任务执行失败。",
                                               "retryable": False, "requires_user_action": False, "user_action": ""})
            self.store.put("job", job)
        finally:
            self.store.db.commit()
            self.tasks.pop(job_id, None)

    def present(self, job: dict) -> dict:
        return {key: job.get(key) for key in (
            "id", "operation", "state", "progress", "result", "error", "checkpoint", "recovery_reason",
        )}

    async def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id, "job")
        if job["state"] not in {"succeeded", "partially_succeeded", "cancelled"}:
            if job_id in self.tasks:
                self.tasks[job_id].cancel()
                await asyncio.gather(self.tasks[job_id], return_exceptions=True)
                self.tasks.pop(job_id, None)
            job = self.store.get(job_id, "job")
            self.store.put("job", {**job, "state": "cancelled"})
            self.store.db.commit()
        return self.present(self.store.get(job_id, "job"))

    def resume(self, job_id: str, session_id: str | None = None) -> dict:
        job = self.store.get(job_id, "job")
        if job["state"] in {"succeeded", "partially_succeeded", "running", "queued", "cancelled"}:
            return self.present(job)
        if job["operation"] == "acquire_fulltext":
            require(session_id == job["arguments"].get("session_id"),
                    "SESSION_MISMATCH", "恢复下载必须使用原会话，或新建具有新幂等键的下载请求。")
            session = self.store.get(session_id, "session")
            require(session["state"] == "ready", "AUTHENTICATION_REQUIRED", "请先验证会话。")
        job.update(state="queued", error=None)
        self.store.put("job", job)
        self.store.db.commit()
        self.schedule(job)
        return self.present(job)

    async def shutdown(self):
        self.stopping = True
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
