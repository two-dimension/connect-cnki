"""Validate source-bound trial criteria; this is not a production MCP endpoint.

The same Agent judgments may be reformatted here, but never become independent
gold labels or evidence of improved semantic accuracy through that conversion.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from evaluate import aggregate, digest


def run(snapshot, protocol, packet):
    ids = {v for query in snapshot["queries"] if query["arguments"]["query"]["text"] != "期权"
           for v in query["version_ids"]}
    assert packet["snapshot_digest"] == digest(snapshot), "Assessments use a different frozen corpus"
    rows = packet["assessments"]
    assert len(rows) == len(ids) and {row["version_id"] for row in rows} == ids
    results = []
    for row in rows:
        paper = snapshot["papers"][row["version_id"]]
        material = paper["title"] + "\n" + (paper.get("abstract") or "")
        observation_ids = {observation["id"] for observation in paper.get("observations", [])}
        for assessment in row["criteria"]:
            for reference in assessment.get("references", []):
                assert reference["version_id"] == row["version_id"], "Cross-paper evidence"
                assert reference.get("observation_id") in observation_ids, "Foreign or missing immutable observation"
                assert reference["quotation"] and reference["quotation"] in material, "Unlocated quotation"
        decision = aggregate(protocol["topic_criteria"], row["criteria"], "title_abstract")
        results.append({**row, "decision": decision, "fulltext_stage": "not_assessed",
                        "background_candidate_retained": row["shared_agent_label"] == "support"})
    return {"snapshot_digest": digest(snapshot), "assessment_digest": digest(packet),
        "origin": packet["origin"], "independent_semantic_accuracy_gain": "not_measurable_from_shared_judgments",
        "counts": dict(Counter(r["decision"]["result"] for r in results)), "rows": results,
        "unknown_is_not_violation": True, "ranking_used_for_exclusion": False,
        "abstract_approval_is_final_inclusion": False, "production_registration": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--assessments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(*(json.loads(path.read_text(encoding="utf-8")) for path in (
        args.snapshot, args.protocol, args.assessments)))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "origin": result["origin"],
                      "semantic_accuracy_gain": result["independent_semantic_accuracy_gain"]}, ensure_ascii=False))
