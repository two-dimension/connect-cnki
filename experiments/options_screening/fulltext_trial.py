"""Recover frozen article entries, then download and parse serially via the MCP queue.

Stops on any source or task failure. Observation timeout leaves the original request
and backend task intact; inspect that same request before restarting this controller.
No new candidates enter the frozen trial or RRF votes. No model calls or PDF rewriting.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from cnki_mcp.core import now


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Controller:
    def __init__(self, root):
        self.root, self.sequence, self.run_id = root, 0, uuid.uuid4().hex[:10]
        self.state_path = root / "fulltext-controller-state.json"
        self.manifest_path = root / "fulltext-manifest.json"
        self.manifest = read(self.manifest_path) if self.manifest_path.exists() else {}
        self.state = {"run_id": self.run_id, "started_at": now(), "state": "running",
                      "completed": sum(bool(row.get("parse")) for row in self.manifest.values())}

    def state_update(self, **values):
        self.state.update(values, at=now())
        write(self.state_path, self.state)

    def call(self, tool, arguments):
        self.sequence += 1
        request = self.root / "queue" / f"038-auto-{self.run_id}-{self.sequence:03d}.request.json"
        processing = request.with_suffix(".processing").name
        baseline = {p.name for p in self.root.glob("response-*.json")}
        self.state_update(tool=tool, pending_request=processing)
        write(request, {"tool": tool, "arguments": arguments})
        started = time.monotonic()
        while time.monotonic() - started < 180:
            for path in sorted(self.root.glob("response-*.json")):
                if path.name not in baseline:
                    envelope = read(path)
                    if envelope.get("request_file") == processing:
                        result = envelope.get("result") or {}
                        self.state_update(last_response=path.name, pending_request=None)
                        if result.get("status") not in {"succeeded", "partially_succeeded"}:
                            raise RuntimeError(json.dumps({"tool": tool, "error": result.get("error"),
                                                           "status": result.get("status")}, ensure_ascii=False))
                        return result["data"]
            time.sleep(0.5)
        raise TimeoutError("Observation timed out; original request/backend may still be live. Do not resubmit blindly.")

    def wait_job(self, job_id):
        started = time.monotonic()
        while time.monotonic() - started < 420:
            job = self.call("get_job", {"job_id": job_id})
            state = job["state"]
            if state in {"succeeded", "partially_succeeded"}:
                return job
            if state in {"failed", "waiting_for_user", "cancelled"}:
                raise RuntimeError(json.dumps({"job_id": job_id, "state": state, "error": job.get("error")}, ensure_ascii=False))
            time.sleep(2)
        raise TimeoutError("Task observation timed out; inspect the same job before continuing.")

    def run(self):
        ctx = read(self.root / "fulltext-active-session.json")
        snapshot = read(self.root / "candidate-snapshot-cc30c617b19b.json")
        original_order = read(self.root / "fulltext-order.json")
        known_available = "version_a13af6a30b934e659686993d0795acbb"
        order = [known_available] + [v for v in original_order if v != known_available]
        write(self.root / "fulltext-recovery-order.json", {"order": order,
            "reason": "Validate known available PDF after default-reader fix; selected ranking and eight-paper sets unchanged"})
        existing_jobs = {}
        for path in sorted(self.root.glob("response-*.json")):
            envelope = read(path)
            if envelope["tool"] == "acquire_fulltext" and (envelope.get("result") or {}).get("data"):
                existing_jobs[envelope["arguments"]["version_id"]] = envelope["result"]["data"]["id"]
        for version_id in order:
            if version_id in self.manifest and self.manifest[version_id].get("parse"):
                continue
            self.state_update(version_id=version_id, phase="restore_normal_entry")
            paper = snapshot["papers"][version_id]
            query = self.call("search_papers", {"project_id": ctx["project_id"], "session_id": ctx["session_id"],
                "query": {"text": paper["title"], "field": "title"}, "sort": "relevance", "limit": 1})
            assert len(query["papers"]) == 1 and query["papers"][0]["id"] == version_id, "Recovered source identity differs"
            self.state_update(phase="download", recovery_query_id=query["query_id"])
            if version_id in existing_jobs:
                submitted = self.call("resume_job", {"job_id": existing_jobs[version_id], "session_id": ctx["session_id"]})
            else:
                submitted = self.call("acquire_fulltext", {"project_id": "project_5f9a351572274924ae5b9c3e2965870e",
                    "session_id": ctx["session_id"], "version_id": version_id,
                    "idempotency_key": "options-trial-pdf-" + version_id})
            downloaded = self.wait_job(submitted["id"])
            asset = downloaded["result"]
            assert asset["version_id"] == version_id
            self.manifest[version_id] = {"title": paper["title"], "source_id": paper["source_id"],
                "download_job_id": submitted["id"], "download_state": downloaded["state"], "asset": asset,
                "recovery_query_id": query["query_id"]}
            write(self.manifest_path, self.manifest)
            self.state_update(phase="parse", asset_id=asset["id"])
            accepted = self.call("parse_fulltext", {"asset_id": asset["id"]})
            parsed = self.wait_job(accepted["id"])
            self.manifest[version_id].update(parse_job_id=accepted["id"], parse_state=parsed["state"], parse=parsed["result"])
            write(self.manifest_path, self.manifest)
            count = sum(bool(row.get("parse")) for row in self.manifest.values())
            self.state_update(completed=count)
            print(json.dumps({"at": now(), "completed": count, "version_id": version_id,
                              "pages": parsed["result"]["page_count"], "parse_state": parsed["state"]}), flush=True)
        self.state_update(state="completed", phase="material_acquisition_only")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    controller = Controller(args.runtime.resolve(strict=True))
    try:
        controller.run()
    except (RuntimeError, AssertionError, TimeoutError, OSError) as error:
        controller.state_update(state="needs_attention", error=str(error))
        print(json.dumps({"state": "needs_attention", "error": str(error)}, ensure_ascii=False), flush=True)
        raise SystemExit(2) from None
