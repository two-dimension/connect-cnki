"""Audit real trial artifacts and emit aggregate metrics; no new relevance scoring.

All source material stays in the ignored runtime. The public JSON contains only
counts, digests, limitations and the conservative adoption decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from cnki_mcp.core import digest, now


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def audit(runtime, public_output):
    ranks = read(runtime / "ranking-results.json")
    frozen = read(runtime / "candidate-snapshot-cc30c617b19b.json")
    assert digest(frozen) == ranks["snapshot_digest"]
    responses = [read(f) for f in sorted(runtime.glob("response-*.json"))]
    projects = read(runtime / "review-projects.json")
    material = read(runtime / "fulltext-manifest.json")
    sections = read(runtime / "review-trial-sections.json")
    source_audit = read(runtime / "review-source-audit.json")
    screening = read(runtime / "structured-screening-results.json")
    assert screening["snapshot_digest"] == ranks["snapshot_digest"]
    assert len(material) == 13 and len(read(runtime / "paper-card-packets.json")) == 13
    for item in material.values():
        assert item["parse_state"] in {"succeeded", "partially_succeeded"}
        assert item["asset"]["sha256"] == item["parse"]["sha256"]
        file = runtime / "library" / "assets" / (item["asset"]["sha256"] + ".pdf")
        assert hashlib.sha256(file.read_bytes()).hexdigest() == item["asset"]["sha256"]
    returned = {}
    for row in responses:
        data = (row.get("result") or {}).get("data") or {}
        if row["tool"] in {"read_paper", "search_evidence"}:
            for chunk in data.get("evidence", data.get("results", [])):
                for eid in chunk.get("evidence_ids", [chunk["id"]]):
                    returned[eid] = chunk
    for card in read(runtime / "paper-card-packets.json").values():
        for field in card["fields"]:
            assert not field["human_checked"]
            for ref in field["references"]:
                for eid in ref["evidence_ids"]:
                    assert eid in returned and returned[eid]["version_id"] == card["version_id"]
    decisions = []
    for version_id, card in read(runtime / "paper-card-packets.json").items():
        decisions.append({"version_id":version_id,"criterion_id":"DESIGN_SUPPORT","stage":"fulltext",
            "judgment":"meets","not_a_quality_score":True,
            "role":"background" if version_id.startswith(("version_cb1c", "version_591d")) else "core",
            "method_references":card["fields"][1 if version_id.startswith("version_a13a") else 0]["references"],
            "result_and_condition_references":[ref for f in card["fields"] for ref in f["references"]],
            "scope":"Only the current review statements and returned methods/results; not certification of every page or finding",
            "pending_questions":card["pending_questions"],"human_checked":False})
    write(runtime / "fulltext-criteria.json", decisions)
    exports, validation, prepares = {}, {}, {}
    for arm, project in projects.items():
        exported = [r for r in responses if r["tool"] == "export_project" and r["arguments"]["project_id"] == project]
        assert {r["arguments"]["format"] for r in exported} == {"json", "markdown"}
        j = next(r for r in exported if r["arguments"]["format"] == "json")
        package = read(Path(j["result"]["data"]["path"]))
        selected = set(ranks["review_priority"][arm][:8])
        assert set(package["review_manifest"]["version_ids"]) == selected
        assert {c["version_id"] for c in package["paper_matrix"]} == selected
        assert len(package["review_manifest"]["entry_ids"]) == 8
        body = package["document"]["body"]
        assert not re.search(r"\[version_", body)
        assert {int(x) for x in re.findall(r"\[(\d+)\]", body)} == set(range(1, 9))
        assert package["document"]["section_revision_ids"] == [sections[arm]["section_id"]]
        for a in package["review_artifacts"]:
            if a["kind"] in {"review_section", "synthesis"}:
                content = a["content"]["value"]
                for claim in content["claims"]:
                    for source in claim["sources"]:
                        ref = source["reference"]
                        for eid in ref["evidence_ids"]:
                            assert eid in returned
                            assert returned[eid]["version_id"] == ref["version_id"]
                            assert returned[eid]["asset_id"] == ref["asset_id"]
                            assert returned[eid]["parse_artifact_id"] == ref["parse_artifact_id"]
                            assert returned[eid]["sha256"] == ref["sha256"]
                            assert not (ref["version_id"].startswith("version_93ce") and returned[eid]["pdf_page"] == 57)
        checks = [r for r in responses if r["tool"] == "save_review_check" and r["arguments"]["project_id"] == project]
        assert len(checks) == 3 and all(r["result"]["status"] == "succeeded" for r in checks)
        support = next(r for r in checks if r["arguments"]["check"]["review_type"] == "support")
        assert support["result"]["data"]["content"]["value"]["sentence_inventory_accounted"]
        progress = [r for r in responses if r["tool"] == "get_review_progress" and r["arguments"]["project_id"] == project][-1]["result"]["data"]
        assert not progress["missing_cards"] and progress["artifact_counts"]["paper_card"] == 8
        assert all(s["dependency_status"] == "unchanged" for s in progress["sections"])
        assert all(s["user_disposition"] == "unresolved" for sec in progress["sections"] for s in sec["sentence_queue"])
        assert 1800 <= source_audit[arm]["text_chars_excluding_citations"] <= 2600
        assert source_audit[arm]["han_characters"] >= 1800
        note = ("本稿为本次有限候选池的研究综述草稿，含调用方 Agent 核对；未接受独立人工评审或计量复算。\n\n")
        if arm == "enhanced":
            assert "2018" in body and "2017" in body
            note += ("书目冲突提示：[8] 原题录年份为2018，但PDF首页标为《中国管理科学》2017年第12期；"
                     "下列书目保留MCP冻结观测，正式引用前应按印刷版本核定，不能把2018视为已确认年份。\n\n")
        else:
            note += "材料限制：价格发现论文PDF第57页未提取；正文只使用其他已返回的方法与结果页。\n\n"
        readable = runtime / (arm + "-review.md")
        readable.write_text(note + body + "\n\n## 参考文献\n\n" + package["formatted_bibliography"] + "\n", encoding="utf-8")
        exports[arm] = {"readable_markdown": str(readable.resolve()),
                        "original_exports": [r["result"]["data"]["path"] for r in exported],
                        "section_id": sections[arm]["section_id"], "sha256": hashlib.sha256(readable.read_bytes()).hexdigest()}
        validation[arm] = source_audit[arm]
        prepare = next(r for r in responses if r["tool"] == "prepare_synthesis" and r["arguments"]["project_id"] == project)
        job = [r for r in responses if r["tool"] == "get_job" and r["arguments"]["job_id"] == prepare["result"]["data"]["id"]][-1]["result"]["data"]
        assert job["state"] == "succeeded" and len(job["result"]["matrix"]) == 8
        prepares[arm] = {"state": job["state"], "matrix_rows": 8,
                         "uncovered_versions": len(job["result"].get("uncovered_version_ids", []))}
        syntheses = [r["result"]["data"] for r in responses if r["tool"]=="save_synthesis" and r["arguments"]["project_id"]==project]
        assert len(syntheses)==2 and syntheses[1]["parent_revision_id"]==syntheses[0]["id"]
        assert syntheses[1]["entity_id"]==syntheses[0]["entity_id"]
        historical = next(r for r in responses if r["tool"]=="get_review_artifact"
                          and r["arguments"]["artifact_id"]==syntheses[0]["id"])["result"]["data"]["artifact"]
        assert historical["content_digest"] == syntheses[0]["content_digest"]
        assert historical["content"]["value"] == syntheses[0]["content"]["value"]
        validation[arm]["initial_synthesis_history_replayed"] = True
    by_tool, statuses, errors = defaultdict(float), Counter(), Counter()
    job_failures, latest_jobs = {}, {}
    times = []
    for r in responses:
        by_tool[r["tool"]] += r["elapsed_ms"]
        result = r.get("result") or {}
        statuses[result.get("status", "protocol_error")] += 1
        if result.get("error"):
            errors[result["error"]["code"]] += 1
        if r["tool"] == "get_job" and result.get("data"):
            job = result["data"]
            latest_jobs[job["id"]] = job
            if job["state"] in {"failed", "waiting_for_user"}:
                job_failures[job["id"]] = (job.get("error") or {}).get("code", job["state"])
        times.append(datetime.fromisoformat(r["at"]))
    intervals = [(b-a).total_seconds() for a,b in zip(times, times[1:], strict=False)]
    ranking = ranks["metrics_against_agent_reference"]
    baseline = ranking[ranks["review_baseline_selected_on_development"]]["held_out"]
    enhanced = ranking["rrf_k60"]["held_out"]
    pdelta = enhanced["10"]["precision_lower"] - baseline["10"]["precision_lower"]
    rdelta = enhanced["20"]["recall_in_labeled_reference"] - baseline["20"]["recall_in_labeled_reference"]
    gates = {"ranking": {"passed": pdelta >= .2 and rdelta >= .15, "precision_at_10_delta": round(pdelta, 6),
                         "bounded_recall_at_20_delta": round(rdelta, 6)},
             "screening": {"passed": False, "status": "not_established_same_agent_judgments_not_independent_error_measurement"},
             "review": {"passed": False, "status": "no_observed_reduction_in_caller_audit_no_independent_semantic_gold"},
             "cost": {"passed": False, "ratio": None, "status": "not_comparably_measured_shared_reading_and_recovery"}}
    assert not any(gates[k]["passed"] for k in ["ranking", "screening", "review"])
    metrics = {"at": now(), "scope": "exploratory_real_full_workflow_not_independent_quality_acceptance",
        "frozen_candidate_digest": ranks["snapshot_digest"], "protocol_version": read(Path(__file__).with_name("protocol.json"))["protocol_version"],
        "acquisition": ranks["acquisition"], "held_out": {"baseline": baseline, "rrf": enhanced},
        "screening": {"counts": screening["counts"], "pending_rate": screening["counts"]["pending_review"]/len(screening["rows"]),
                      "background_retained": ranks["acquisition"]["multi_query"]["agent_labels"]["support"],
                      "independent_false_exclusions": None},
        "fulltext": {"selected_unique": 13, "per_arm": 8, "shared": 3, "replacements": 0,
                     "page_count": sum(x["parse"]["page_count"] for x in material.values()),
                     "known_failed_pages": 1, "preprocessing_page_budget_adjusted": {"initial":200,"trial":500,"production_default":200},
                     "claim_specific_reading_complete": True,"whole_documents_understood_certified":False},
        "reviews": validation, "preparation_jobs": prepares,
        "cost": {"calls":len(responses),"by_tool":dict(Counter(r["tool"] for r in responses)),
                 "tool_elapsed_ms":{k:round(v,3) for k,v in by_tool.items()},"response_statuses":dict(statuses),
                 "error_codes":dict(errors),"call_timestamp_span_seconds":(times[-1]-times[0]).total_seconds(),
                 "distinct_jobs_with_observed_failure_or_wait":len(job_failures),
                 "observed_job_failure_codes":dict(Counter(job_failures.values())),
                 "last_observed_job_states":dict(Counter(j["state"] for j in latest_jobs.values())),
                 "intervals_over_15_minutes_seconds":[round(t,3) for t in intervals if t>900],
                 "timestamp_intervals_are_not_processing_time":True,"background_task_and_human_time_ratio":None,
                 "comparability":"material reading shared; no independent arm timers, cannot prove <=1.25"},
        "gates": gates,"adopt_new_screening_mechanisms":False,
        "production_selection_tools_added":[],"shared_acquisition_parsing_fixes_retained":True,
        "untested_backends":["BGE reranker","ASReview active learning"],"human_quality_acceptance":"not_performed"}
    root = Path(__file__).resolve().parents[2]
    build_dir = runtime / "package-final"
    wheel = next(build_dir.glob("*.whl"))
    sdist = next(build_dir.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in names:
            if name.startswith("cnki_mcp/") and name.endswith((".py", ".csl")):
                assert archive.read(name) == (root / "src" / name).read_bytes(), name
        assert b"STREAM_LIMIT_ERROR" in archive.read("cnki_mcp/pdf_worker.py")
    with tarfile.open(sdist) as archive:
        names += ["/".join(name.split("/")[1:]) for name in archive.getnames()]
    assert all(not n.startswith(("data/", ".venv/", "imports/", "node_modules/")) for n in names)
    assert all(not n.endswith((".pdf", ".sqlite3", "config.local.json", ".env")) for n in names)
    assert "experiments/options_screening/finalize_trial.py" in names
    protocol = read(runtime / "final-protocol-validation.json")
    assert protocol["tool_count"] == 39 and protocol["prompt_count"] == 3 and protocol["resource_entries"] == 3
    assert "81 passed" in (runtime / "final-pytest.log").read_text(encoding="utf-8-sig")
    metrics["engineering_validation"] = {"tests_passed":81,"production_tests":76,"trial_analysis_tests":5,
        "real_stdio_tools":39,"prompts":3,"resources":3,"wheel_sources_match":True,
        "sdist_and_wheel_exclude_runtime_and_credentials":True,
        "package_artifacts":[f.name for f in [wheel,sdist]]}
    write(runtime / "package-validation.json", {"at":now(),
        "artifact_hashes":{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [wheel,sdist]},
        "wheel_sources_match":True,"runtime_excluded":True})
    write(runtime / "review-export-index.json", exports)
    write(runtime / "final-trial-metrics.json", metrics)
    write(public_output, metrics)
    print(json.dumps({"adopt":False,"verified_pdfs":13,"cards":16,"review_sentences":53,
                      "mcp_calls":len(responses),"readable_exports":[x["readable_markdown"] for x in exports.values()]},ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("final-metrics.json"))
    args = parser.parse_args()
    audit(args.runtime.resolve(), args.output.resolve())
