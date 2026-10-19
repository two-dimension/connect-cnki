from __future__ import annotations

import asyncio

from .core import digest, require
from .writing import WRITING_VERSION, writing_prompt


class ReviewPreparation:
    """Resumable local material assembly; never runs an Agent or an external operation."""

    def __init__(self, workspace, evidence, jobs, settings):
        self.workspace, self.evidence, self.jobs, self.settings = workspace, evidence, jobs, settings
        self.store = workspace.store

    def submit(self, project_id, question, snapshot_id, version_ids=None, plan_id=None, synthesis=False):
        project = self.workspace.project(project_id, writing=True)
        snapshot = self.workspace.corpus(project_id, snapshot_id)
        items = snapshot["content"]["items"]
        ids = version_ids if version_ids is not None else [i["version_id"] for i in items]
        require(ids and len(ids) == len(set(ids)) and set(ids) <= {i["version_id"] for i in items},
                "PROJECT_MISMATCH", "材料准备文献需来自同一快照且不重复。")
        plan_id = plan_id or snapshot["content"].get("plan_id")
        plan = self.workspace.get(project_id, plan_id, "review_plan") if plan_id else None
        cards = [c for c in self.workspace.cards_for_snapshot(project_id, snapshot["id"]) if c["content"]["version_id"] in ids]
        arguments = {"project_id": project_id, "snapshot_id": snapshot["id"], "version_ids": ids,
            "plan_id": plan["id"] if plan else None, "question": question or project["question"],
            "card_ids": [c["id"] for c in cards], "generation": project.get("review_generation", 0),
            "max_read_chars": self.settings.max_read_chars, "writing_rules_version": WRITING_VERSION}
        operation = "prepare_synthesis" if synthesis else "prepare_review_v2"
        return self.jobs.submit(operation, arguments, digest([operation, arguments]))

    async def run(self, job):
        args = job["arguments"]
        project_id = args["project_id"]
        snapshot = self.workspace.corpus(project_id, args["snapshot_id"])
        checkpoint = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
        rows = list(checkpoint.get("rows", []))
        remaining = checkpoint.get("remaining", args["max_read_chars"])
        delivered_ids = list(checkpoint.get("evidence_ids", []))
        observation_ids = list(checkpoint.get("observation_ids", []))
        for index in range(checkpoint.get("next_index", 0), len(args["version_ids"])):
            current = self.store.get(job["id"], "job")
            project = self.workspace.project(project_id)
            if not project.get("review_assistance_enabled", False) or project.get("review_generation", 0) != current.get("resume_generation", args["generation"]):
                return {"_job_state": "waiting_for_user", "_checkpoint": current.get("checkpoint"),
                        "_progress": index / len(args["version_ids"]), "_recovery_reason": "review_assistance_disabled",
                        "snapshot_id": snapshot["id"], "completed_units": index, "resume_required": True}
            version_id = args["version_ids"][index]
            item = self.workspace.item(snapshot, version_id)
            if item["bibliography"].get("abstract"):
                observation_ids.append(item["observation_id"])
            cards = [self.store.get(c, "paper_card") for c in args["card_ids"]
                     if self.store.get(c, "paper_card")["content"]["version_id"] == version_id]
            allocation = min(remaining, max(1200, remaining // (len(args["version_ids"]) - index))) if remaining else 0
            result = self.evidence.search(project_id, args["question"], top_k=2, snapshot_id=snapshot["id"],
                version_ids=[version_id], context_blocks=1, plan_id=args["plan_id"], max_chars=allocation,
                record_progress=False) if allocation else {"results": [], "returned_chars": 0}
            remaining -= result["returned_chars"]
            for span in result["results"]:
                delivered_ids.extend(span.get("evidence_ids", []))
            rows.append({"version_id": version_id, "bibliography": self.workspace.present_item(item)["bibliography"],
                "reading_level": item["reading_level"], "quality": item["quality"],
                "cards": [self.workspace.present(c) for c in cards], "evidence": result["results"],
                "needs_agent_analysis": not bool(cards), "next_reading_required": not bool(result["results"]),
                "support_status": "not_assessed"})
            checkpoint = {"next_index": index + 1, "rows": rows, "remaining": remaining,
                          "evidence_ids": sorted(set(delivered_ids)), "observation_ids": sorted(set(observation_ids))}
            current = self.store.get(job["id"], "job")
            self.store.put("job", {**current, "checkpoint": checkpoint,
                                   "progress": (index + 1) / len(args["version_ids"])})
            self.store.db.commit()
            await asyncio.sleep(0)
        section_context = [self.workspace.present(s) for s in self.workspace.heads(project_id, "review_section")
                           if s["content"]["snapshot_id"] == snapshot["id"]]
        return {"snapshot_id": snapshot["id"], "matrix": rows, "existing_sections": section_context,
            "plan": self.workspace.present(self.store.get(args["plan_id"])) if args["plan_id"] else None,
            "uncovered_version_ids": [r["version_id"] for r in rows if r["next_reading_required"]],
            "instructions": writing_prompt(), "writing_rules_version": WRITING_VERSION,
            "evidence_truncated": any(r["next_reading_required"] for r in rows),
            "model_calls": 0, "support_status": "not_certified", "_evidence_ids": sorted(set(delivered_ids)),
            "_observation_ids": sorted(set(observation_ids))}

    def record_delivery(self, job):
        if job["operation"] not in {"prepare_review_v2", "prepare_synthesis"} or not job.get("result") or job.get("reading_delivered"):
            return
        if not self.workspace.project(job["arguments"]["project_id"]).get("review_assistance_enabled", False):
            return
        ids = job["result"].get("_evidence_ids", [])
        spans = [self.store.get(i, "evidence") for i in ids]
        snapshot = self.workspace.corpus(job["arguments"]["project_id"], job["arguments"]["snapshot_id"])
        self.evidence.record_return(job["arguments"]["project_id"], snapshot, self.evidence.merged(spans))
        for observation_id in job["result"].get("_observation_ids", []):
            item = next(i for i in snapshot["content"]["items"] if i["observation_id"] == observation_id)
            if self.workspace.gate.can_output("abstract", item["metadata_rights"]):
                self.workspace.revision(job["arguments"]["project_id"], "reading_progress", {
                    "snapshot_id": snapshot["id"], "observation_id": observation_id, "version_id": item["version_id"],
                    "meaning": "returned_to_client_not_understanding"}, dependencies=[snapshot["id"], observation_id])
        self.store.put("job", {**job, "reading_delivered": True})
