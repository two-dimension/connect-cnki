"""Persist declared Agent relevance annotations with exact, policy-filtered material witnesses."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from evaluate import digest


def append(runtime, rows):
    path = runtime / "reference-labels.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for row in rows:
        response = runtime / f"response-{row['response']:04d}.json"
        envelope = json.loads(response.read_text(encoding="utf-8"))
        assert envelope["tool"] == "get_paper" and envelope["result"]["status"] == "succeeded"
        material = envelope["result"]["data"]
        paper = material["paper"]
        abstract = paper.get("abstract") if isinstance(paper.get("abstract"), str) else ""
        assert row["label"] in {"core", "support", "outside", "uncertain"}
        assert row["quote"] in paper["title"] or row["quote"] in abstract
        version_id = paper["id"]
        assert version_id not in saved, "Use an explicit adjudication revision, never silently overwrite a reference label"
        saved[version_id] = {**row, "version_id": version_id, "material_response": response.name,
            "material_digest": digest(material), "evaluator_kind": "agent", "independent_human_gold": False,
            "model_checkpoint": "not_exposed_by_calling_client", "prompt_version": "topic-reference-20261020.1",
            "source_observation_id": material["observations"][-1]["id"],
            "at": datetime.now(UTC).isoformat(), "rank_or_score_visible": False}
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"annotations": len(saved), "new": len(rows), "independent_human_gold": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(append(args.runtime.resolve(), json.loads(args.annotations.read_text(encoding="utf-8")))), flush=True)
