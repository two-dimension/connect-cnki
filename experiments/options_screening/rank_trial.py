"""Evaluate frozen, untrained rankings after declared Agent relevance annotation."""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from evaluate import digest, ranking_metrics, rrf

from cnki_mcp.core import normalize
from cnki_mcp.review_evidence import ReviewEvidence


def run(snapshot_path, labels_path, output):
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    annotations = json.loads(labels_path.read_text(encoding="utf-8"))
    labels = {v: a["label"] for v, a in annotations.items()}
    assert set(labels) == set(snapshot["papers"]), "Every frozen candidate needs an explicit label, including uncertain"
    multi = [q for q in snapshot["queries"] if q["arguments"]["query"]["text"] != "期权"]
    broad = [q for q in snapshot["queries"] if q["arguments"]["query"]["text"] == "期权"][0]
    union = list(dict.fromkeys(v for q in multi for v in q["version_ids"]))
    started = time.perf_counter_ns()
    fused, scores = rrf(multi)
    rrf_ns = time.perf_counter_ns() - started
    question = "期权市场微观结构与投资者行为"
    exact, words, grams = ReviewEvidence.terms(question, {})
    started = time.perf_counter_ns()
    lexical_scores = {}
    for version_id in union:
        paper = snapshot["papers"][version_id]
        abstract = paper.get("abstract") if isinstance(paper.get("abstract"), str) else ""
        text = normalize(paper["title"] + "\n" + abstract)
        lexical_scores[version_id] = (10 * sum(text.count(t) for t in exact)
            + 3 * sum(text.count(t) for t in words) + sum(text.count(t) for t in grams))
    positions = {v: i for i, v in enumerate(union)}
    lexical = sorted(union, key=lambda v: (-lexical_scores[v], positions[v]))
    lexical_ns = time.perf_counter_ns() - started
    rankings = {"query_union_first_seen": union, "literal_title_abstract": lexical, "rrf_k60": fused}
    groups = {p["version_id"]: p["split"] for p in snapshot["blind_packet"]}
    references = {"all_multi_candidates": set(union),
                  "development": {v for v in union if groups[v] == "development"},
                  "held_out": {v for v in union if groups[v] == "held_out"}}
    metrics = {name: {split: {str(k): ranking_metrics(order, labels, k, ids) for k in (10, 20)}
                     for split, ids in references.items()} for name, order in rankings.items()}
    # Choose the operational review comparator from development only, never from holdout scores.
    comparator = max(("query_union_first_seen", "literal_title_abstract"),
                     key=lambda name: (metrics[name]["development"]["10"]["precision_lower"],
                                       name == "literal_title_abstract"))
    packet = {"snapshot": str(snapshot_path), "snapshot_digest": digest(snapshot), "reference_digest": digest(annotations),
        "reference_origin": "single_calling_agent_not_independent_human_gold", "blinding": "not_strict_double_blind",
        "rankings": rankings, "scores": {"rrf_k60": scores, "literal_title_abstract": lexical_scores},
        "metrics_against_agent_reference": metrics, "review_baseline_selected_on_development": comparator,
        "review_priority": {"baseline": [v for v in rankings[comparator] if labels[v] == "core"],
                            "enhanced": [v for v in fused if labels[v] == "core"]},
        "acquisition": {"single_query": {"raw": len(broad["version_ids"]), "unique": len(set(broad["version_ids"])),
                                           "agent_labels": dict(Counter(labels[v] for v in set(broad["version_ids"])))},
            "multi_query": {"raw": sum(len(q["version_ids"]) for q in multi), "unique": len(union),
                            "agent_labels": dict(Counter(labels[v] for v in union))},
            "candidate_union": len(snapshot["papers"]), "overlap": len(set(union) & set(broad["version_ids"]))},
        "implementation": {"lexical": "production tokenization/normalization adapted to title+abstract; fixed 10/3/1 weights",
            "rrf": "k60; identical executed queries vote once; within-list dedup; no score exclusion"},
        "timing": {"rrf_ns": rrf_ns, "lexical_ns": lexical_ns, "scope": "single local ranking pass, excludes reading/writing; not labor-saving evidence"},
        "adoption_decision": "pending_full_workflow_and_evidence_audit", "production_changes": False}
    output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {"acquisition": packet["acquisition"], "review_baseline": comparator,
        "held_out_P10_bounds": {name: [m["held_out"]["10"]["precision_lower"], m["held_out"]["10"]["precision_upper"]]
                                for name, m in metrics.items()},
        "review_top8": {arm: [{"version_id": v, "title": snapshot["papers"][v]["title"]} for v in order[:8]]
                        for arm, order in packet["review_priority"].items()}, "reference_origin": packet["reference_origin"]}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot, args.labels, args.output), ensure_ascii=False, indent=2))
