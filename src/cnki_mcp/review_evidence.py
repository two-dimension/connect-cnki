from __future__ import annotations

import re

from .core import normalize, require
from .policy import protected
from .review_workspace import ReviewWorkspace


class ReviewEvidence:
    def __init__(self, workspace: ReviewWorkspace, settings):
        self.workspace, self.settings = workspace, settings
        self.store, self.gate = workspace.store, workspace.gate

    def _sections(self, project_id, snapshot_id, version_id, section_name):
        if not section_name:
            return None
        cards = [c for c in self.workspace.cards_for_snapshot(project_id, snapshot_id) if c["content"]["version_id"] == version_id]
        ranges = [s for c in cards for s in c["content"]["sections"] if s["name"] == section_name]
        require(ranges, "SECTION_RANGE_UNKNOWN", "没有已登记的该章节页段。")
        return {p for s in ranges for p in range(s["page_from"], s["page_to"] + 1)}

    @staticmethod
    def merged(spans):
        """Remove only identical positional overlap; preserve all original evidence identities."""
        groups = []
        for span in sorted(spans, key=lambda e: (e["version_id"], e["parse_artifact_id"], e["pdf_page"], e["offset"], e["id"])):
            last = groups[-1] if groups else None
            if last and all(last[k] == span[k] for k in ("version_id", "parse_artifact_id", "pdf_page")):
                overlap = last["offset"] + len(last["text"]) - span["offset"]
                if overlap >= 0 and (overlap == 0 or last["text"][-overlap:] == span["text"][:overlap]):
                    last["text"] += span["text"][max(0, overlap):]
                    last["evidence_ids"].append(span["id"])
                    last["source_ranges"].append({"evidence_id": span["id"], "offset": span["offset"], "length": len(span["text"])})
                    continue
            groups.append({**span, "evidence_ids": [span["id"]],
                "source_ranges": [{"evidence_id": span["id"], "offset": span["offset"], "length": len(span["text"])}]})
        return groups

    def present_spans(self, spans):
        return [{**{k: v for k, v in span.items() if k not in {"rights", "text"}},
                 "text": protected(span["text"], "fulltext", span["rights"])} for span in spans]

    def record_return(self, project_id, snapshot, spans):
        # Evaluate the same gate as the endpoint, and never record withheld text as returned.
        if not self.workspace.project(project_id).get("review_assistance_enabled", False):
            return
        visible = [s for s in spans if self.gate.can_output("fulltext", s["rights"])]
        if not visible:
            return
        ranges = [{k: s[k] for k in ("version_id", "asset_id", "parse_artifact_id", "sha256", "pdf_page", "offset", "evidence_ids")}
                  | {"length": len(s["text"])} for s in visible]
        self.workspace.revision(project_id, "reading_progress", {"snapshot_id": snapshot["id"],
            "ranges": ranges, "meaning": "returned_to_client_not_understanding"}, dependencies=[snapshot["id"]])

    def read(self, project_id, snapshot_id, version_id, pages=None, section_name=None, evidence_offset=0,
             max_chars=None, record_progress=True):
        snapshot = self.workspace.corpus(project_id, snapshot_id)
        item = self.workspace.item(snapshot, version_id)
        require(item["parse_artifact_id"], "FULLTEXT_NOT_PARSED", "选定快照没有全文解析产物。")
        artifact = self.store.get(item["parse_artifact_id"], "parse")
        section_pages = self._sections(project_id, snapshot["id"], version_id, section_name)
        pages = pages or sorted(section_pages) if section_pages else pages or [1]
        require(len(pages) <= 20 and all(1 <= p <= artifact["page_count"] for p in pages),
                "INVALID_ARGUMENT", "PDF 页段超范围或超过 20 页。")
        if section_pages:
            pages = [p for p in pages if p in section_pages]
        budget = min(max_chars or self.settings.max_read_chars, self.settings.max_read_chars)
        spans = [e for e in self.store.all("evidence") if e["parse_artifact_id"] == item["parse_artifact_id"]
                 and e["pdf_page"] in pages]
        spans.sort(key=lambda e: (e["pdf_page"], e["offset"], e["id"]))
        selected, used, next_offset = [], 0, evidence_offset
        for index in range(evidence_offset, len(spans)):
            candidate = self.merged([*selected, spans[index]])
            length = sum(len(s["text"]) for s in candidate)
            if length > budget:
                break
            selected.append(spans[index])
            used, next_offset = length, index + 1
        merged = self.merged(selected)
        if record_progress:
            self.record_return(project_id, snapshot, merged)
        return {"snapshot_id": snapshot["id"], "version_id": version_id, "asset_id": item["asset_id"],
            "parse_artifact_id": item["parse_artifact_id"], "sha256": item["sha256"], "quality": artifact["quality"],
            "reading_level": item["reading_level"], "evidence": self.present_spans(merged), "returned_chars": used,
            "truncated": next_offset < len(spans), "next_evidence_offset": next_offset if next_offset < len(spans) else None,
            "budget_too_small": next_offset == evidence_offset and next_offset < len(spans),
            "needs_ocr_or_manual_review": artifact["quality"] != "text_extracted_unreviewed"}

    @staticmethod
    def terms(query, synonyms):
        phrases = [query]
        for key, alternatives in synonyms.items():
            if normalize(key) in normalize(query):
                phrases.extend(alternatives)
        exact = {normalize(p) for p in phrases if normalize(p)}
        words = {normalize(w) for p in phrases for w in re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]+", p)}
        grams = {w[i:i + 2] for w in words if re.search(r"[\u3400-\u9fff]", w) for i in range(len(w) - 1)}
        return exact, words, grams

    def search(self, project_id, query, top_k=5, snapshot_id=None, version_ids=None, section_name=None,
               reading_levels=None, context_blocks=0, plan_id=None, max_chars=None, record_progress=True):
        snapshot = self.workspace.corpus(project_id, snapshot_id)
        items = snapshot["content"]["items"]
        if version_ids:
            require(set(version_ids) <= {i["version_id"] for i in items}, "PROJECT_MISMATCH", "检索文献不在快照内。")
            items = [i for i in items if i["version_id"] in version_ids]
        if reading_levels:
            items = [i for i in items if i["reading_level"] in reading_levels]
        plans = self.workspace.heads(project_id, "review_plan")
        plan = self.workspace.get(project_id, plan_id, "review_plan") if plan_id else (
            self.workspace.get(project_id, snapshot["content"]["plan_id"], "review_plan") if snapshot["content"].get("plan_id")
            else plans[-1] if plans else None)
        exact, words, grams = self.terms(query, plan["content"]["synonyms"] if plan else {})
        def score(text):
            text = normalize(text)
            return 10 * sum(text.count(t) for t in exact) + 3 * sum(text.count(t) for t in words) + sum(text.count(t) for t in grams)
        scored, abstract_results = [], []
        for item in items:
            if item["parse_artifact_id"] and item["reading_level"] == "fulltext":
                allowed_pages = self._sections(project_id, snapshot["id"], item["version_id"], section_name)
                spans = [e for e in self.store.all("evidence") if e["parse_artifact_id"] == item["parse_artifact_id"]
                         and (allowed_pages is None or e["pdf_page"] in allowed_pages)]
                spans.sort(key=lambda e: (e["pdf_page"], e["offset"], e["id"]))
                for index, span in enumerate(spans):
                    value = score(span["text"])
                    if value:
                        neighbors = [e for e in spans[max(0, index-context_blocks):index+context_blocks+1]
                                     if e["pdf_page"] == span["pdf_page"]]
                        scored.append((value, span["id"], neighbors))
            elif not section_name:
                text = item["bibliography"].get("abstract") or item["bibliography"]["title"]
                value = score(text)
                if value:
                    abstract_results.append({"version_id": item["version_id"], "observation_id": item["observation_id"],
                        "reading_level": item["reading_level"], "score": value,
                        "text": protected(text, "abstract" if item["bibliography"].get("abstract") else "metadata", item["metadata_rights"]),
                        "_raw_length": len(text), "_rights": item["metadata_rights"]})
        scored.sort(key=lambda s: (-s[0], s[1]))
        budget = min(max_chars or self.settings.max_read_chars, self.settings.max_read_chars)
        selected, seen, matches, truncated = [], set(), 0, False
        for _, _, neighbors in scored:
            fresh = [s for s in neighbors if s["id"] not in seen]
            if not fresh:
                continue
            if matches == top_k:
                truncated = True
                break
            candidate = self.merged([*selected, *fresh])
            if sum(len(s["text"]) for s in candidate) > budget:
                truncated = True
                continue
            selected.extend(fresh)
            seen.update(s["id"] for s in fresh)
            matches += 1
        merged = self.merged(selected)
        used = sum(len(s["text"]) for s in merged)
        abstracts = []
        for result in sorted(abstract_results, key=lambda r: -r["score"]):
            if matches >= top_k or used + result["_raw_length"] > budget:
                truncated = True
                continue
            used += result["_raw_length"]
            matches += 1
            abstracts.append({k: v for k, v in result.items() if not k.startswith("_")})
            if record_progress and self.workspace.project(project_id).get("review_assistance_enabled", False) and self.gate.can_output(
                    "abstract" if result["reading_level"] == "abstract_only" else "metadata", result["_rights"]):
                self.workspace.revision(project_id, "reading_progress", {"snapshot_id": snapshot["id"],
                    "observation_id": result["observation_id"], "version_id": result["version_id"],
                    "meaning": "returned_to_client_not_understanding"}, dependencies=[snapshot["id"], result["observation_id"]])
        if record_progress:
            self.record_return(project_id, snapshot, merged)
        covered = {s["version_id"] for s in merged} | {r["version_id"] for r in abstracts}
        return {"snapshot_id": snapshot["id"], "results": self.present_spans(merged) + abstracts,
            "method": "literal_words_chinese_bigrams_plan_synonyms_v1", "semantic_search": "not_enabled",
            "truncated": truncated, "returned_chars": used,
            "uncovered_version_ids": [i["version_id"] for i in items if i["version_id"] not in covered],
            "coverage_certified": False}
