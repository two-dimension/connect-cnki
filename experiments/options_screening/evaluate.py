"""Offline experimental analysis. No production registration and no model invocation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized(value):
    return re.sub(r"[\W_]+", "", value.casefold())


def query_key(arguments):
    return digest({k: arguments.get(k) for k in ("query", "filters", "sort", "filter_mode")})


def rrf(lists, k=60):
    scores, first_seen, seen_queries = defaultdict(float), {}, set()
    for entry in lists:
        if entry["query_key"] in seen_queries:
            continue
        seen_queries.add(entry["query_key"])
        unique = list(dict.fromkeys(entry["version_ids"]))
        for rank, version_id in enumerate(unique, 1):
            first_seen.setdefault(version_id, len(first_seen))
            scores[version_id] += 1 / (k + rank)
    return sorted(scores, key=lambda v: (-scores[v], first_seen[v])), dict(scores)


def aggregate(criteria, assessments, stage):
    """Unknown is never negative, ranking is never eligibility, mandatory NA never bypasses scope."""
    expected = {c["criterion_id"]: c for c in criteria if c["stage"] == stage or stage == "fulltext"}
    supplied = {a["criterion_id"]: a for a in assessments}
    if len(supplied) != len(assessments) or not set(supplied) <= set(expected):
        raise ValueError("duplicate or unknown criteria")
    violated, unknown, not_applicable = [], [], []
    for criterion_id, criterion in expected.items():
        assessment = supplied.get(criterion_id)
        judgment = assessment["judgment"] if assessment else "unknown"
        if judgment not in {"meets", "violates", "unknown", "not_applicable"}:
            raise ValueError("invalid judgment")
        if assessment and judgment != "unknown" and not assessment.get("references"):
            raise ValueError("unbound judgment")
        if judgment == "not_applicable":
            if criterion["required"]:
                raise ValueError("mandatory criterion cannot be bypassed")
            if not assessment.get("reason"):
                raise ValueError("NA requires reason")
            not_applicable.append(criterion_id)
        elif judgment == "violates":
            if assessment.get("basis") != "explicit_conflict":
                unknown.append(criterion_id)
            elif criterion["required"]:
                violated.append(criterion_id)
        elif judgment == "unknown" and criterion["required"]:
            unknown.append(criterion_id)
    result = "excluded" if violated else "pending_review" if unknown else "eligible" if stage == "fulltext" else "advance_to_fulltext"
    return {"stage": stage, "result": result, "violations": violated, "unknown": unknown,
            "not_applicable": not_applicable, "semantic_certification": False}


def ranking_metrics(ranking, labels, k, reference_ids=None):
    reference = set(reference_ids if reference_ids is not None else labels)
    ranking = [v for v in dict.fromkeys(ranking) if v in reference]
    top = ranking[:k]
    positive = {v for v in reference if labels.get(v) == "core"}
    uncertain = {v for v in reference if labels.get(v) in {None, "uncertain"}}
    hit = len(set(top) & positive)
    denominator = len(top)
    return {"k": k, "returned": denominator, "positive": hit,
        "uncertain": len(set(top) & uncertain), "precision_lower": hit / denominator if denominator else None,
        "precision_upper": (hit + len(set(top) & uncertain)) / denominator if denominator else None,
        "recall_in_labeled_reference": hit / len(positive) if positive and not uncertain else None,
        "known_positive_reference_size": len(positive), "unresolved_reference_size": len(uncertain),
        "reference_size": len(reference), "global_database_recall": "not_measured"}


def freeze(runtime, protocol_path):
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    responses = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(runtime.glob("response-*.json"))]
    papers, queries, details = {}, {}, {}
    for response in responses:
        result = response.get("result") or {}
        if result.get("status") != "succeeded":
            continue
        data = result.get("data") or {}
        if response["tool"] == "search_papers":
            query_id = data["query_id"]
            entry = queries.setdefault(query_id, {"query_id": query_id, "query_key": query_key(response["arguments"]),
                "arguments": response["arguments"], "version_ids": [], "response_files": [], "elapsed_ms": 0})
            entry["version_ids"].extend(p["id"] for p in data["papers"])
            entry["response_files"].append(response["at"])
            entry["elapsed_ms"] += response["elapsed_ms"]
            entry["stop_reason"] = data.get("stop_reason")
            entry["source_pages"] = data.get("pages_read")
            for paper in data["papers"]:
                papers[paper["id"]] = paper
        elif response["tool"] == "get_paper":
            details[data["paper"]["id"]] = data
    for version_id, detail in details.items():
        if version_id in papers:
            papers[version_id] = {**detail["paper"], "observations": detail["observations"]}
    version_ids = sorted(papers)
    parent = {v: v for v in version_ids}
    def find(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v
    for index, left in enumerate(version_ids):
        for right in version_ids[:index]:
            a, b = papers[left], papers[right]
            same = a.get("work_id") == b.get("work_id") or (a.get("source_id") and a.get("source_id") == b.get("source_id"))
            same = same or (a.get("doi") and a.get("doi") == b.get("doi"))
            same = same or SequenceMatcher(None, normalized(a["title"]), normalized(b["title"])).ratio() >= .94
            if same:
                parent[find(left)] = find(right)
    groups = defaultdict(list)
    for version_id in version_ids:
        groups[find(version_id)].append(version_id)
    group_ids = {v: digest(sorted(papers[x].get("source_id") or normalized(papers[x]["title"]) for x in members))
                 for members in groups.values() for v in members}
    blind = []
    for version_id, paper in papers.items():
        group_id = group_ids[version_id]
        blind.append({"blind_id": "paper_" + digest(paper.get("source_id") or version_id)[:12],
            "version_id": version_id, "group_id": group_id,
            "split": "held_out" if int(digest([protocol["protocol_version"], group_id])[:8], 16) % 3 == 0 else "development",
            "title": paper["title"], "authors": paper.get("authors", []), "year": paper.get("year"),
            "source": paper.get("source"), "paper_type": paper.get("paper_type"), "abstract": paper.get("abstract"),
            "abstract_status": paper.get("abstract_status"), "observations": paper.get("observations", []),
            "material_digest": digest(paper)})
    blind.sort(key=lambda p: digest([protocol["protocol_version"], p["blind_id"]]))
    snapshot = {"protocol_digest": digest(protocol), "queries": list(queries.values()), "papers": papers,
                "blind_packet": blind, "groups": list(groups.values()), "labels_seen": False}
    target = runtime / f"candidate-snapshot-{digest(snapshot)[:12]}.json"
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    (runtime / "blind-packet.json").write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"snapshot": str(target), "snapshot_digest": digest(snapshot), "papers": len(papers),
            "query_lists": len(queries), "group_count": len(groups), "detail_records": len(details),
            "held_out": sum(p["split"] == "held_out" for p in blind)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze"])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=Path(__file__).with_name("protocol.json"))
    args = parser.parse_args()
    print(json.dumps(freeze(args.runtime.resolve(), args.protocol.resolve()), ensure_ascii=False))
