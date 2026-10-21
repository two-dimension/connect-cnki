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
            if last and all(last[k] == span[k] for k in ("version_id", "parse_artifact_id", "pdf_page", "rights")):
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
        result, merged = self.read_artifact(artifact, pages, evidence_offset, max_chars)
        result.update(snapshot_id=snapshot["id"], reading_level=item["reading_level"])
        if result["continuation"]:
            result["continuation"].update(project_id=project_id, snapshot_id=snapshot["id"], section_name=section_name)
        if record_progress:
            self.record_return(project_id, snapshot, merged)
        return result

    def read_artifact(self, artifact, pages=None, evidence_offset=0, max_chars=None):
        pages = sorted(set([1] if pages is None else pages))
        require(len(pages) <= 20 and all(1 <= p <= artifact["page_count"] for p in pages),
                "INVALID_ARGUMENT", "PDF 页序号超范围或请求页数过多。")
        budget = min(self.settings.max_read_chars if max_chars is None else max_chars, self.settings.max_read_chars)
        spans = [e for e in self.store.find("evidence", parse_artifact_id=artifact["id"]) if e["pdf_page"] in pages]
        spans.sort(key=lambda e: (e["pdf_page"], e["offset"], e["id"]))
        require(0 <= evidence_offset <= len(spans), "INVALID_ARGUMENT", "证据读取偏移超范围。")
        selected, used, next_offset = [], 0, evidence_offset
        for index in range(evidence_offset, len(spans)):
            candidate = self.merged([*selected, spans[index]])
            length = sum(len(s["text"]) for s in candidate)
            if length > budget:
                break
            selected.append(spans[index])
            used, next_offset = length, index + 1
        merged = self.merged(selected)
        too_small = next_offset == evidence_offset and next_offset < len(spans)
        has_next = next_offset < len(spans)
        continuation = {"version_id": artifact["version_id"], "asset_id": artifact["asset_id"],
                        "parse_artifact_id": artifact["id"], "pages": pages, "evidence_offset": next_offset}
        return {"version_id": artifact["version_id"], "asset_id": artifact["asset_id"],
            "parse_artifact_id": artifact["id"], "sha256": artifact["sha256"], "quality": artifact["quality"],
            "reading_level": "fulltext" if merged else "metadata_only", "evidence": self.present_spans(merged),
            "selected_chars": used,
            "returned_chars": sum(len(s["text"]) for s in merged if self.gate.can_output("fulltext", s["rights"])),
            "unavailable_pdf_pages": sorted(set(pages).intersection(artifact.get("extraction_failed_pages", []))),
            "truncated": has_next, "next_evidence_offset": next_offset if has_next and not too_small else None,
            "continuation": continuation if has_next and not too_small else None,
            "budget_too_small": too_small, "required_chars": len(spans[next_offset]["text"]) if too_small else None,
            "needs_ocr_or_manual_review": artifact["quality"] != "text_extracted_unreviewed"}, merged

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

    @staticmethod
    def excerpt(span, budget, terms):
        """Locate a literal hit and preserve the precise original page coordinates."""
        if budget <= 0:
            return None
        characters, positions = [], []
        for index, character in enumerate(span["text"]):
            normalized = normalize(character)
            characters.extend(normalized)
            positions.extend([index] * len(normalized))
        text = "".join(characters)
        for group in terms:
            for term in sorted(group, key=lambda t: (-len(t), t)):
                found = text.find(term)
                if found < 0:
                    continue
                left, right = positions[found], positions[found + len(term) - 1] + 1
                if right - left > budget:
                    continue
                start = max(0, min(left - (budget - (right - left)) // 2, len(span["text"]) - budget))
                excerpt = span["text"][start:start + budget]
                if term in normalize(excerpt):
                    return {**span, "offset": span["offset"] + start, "text": excerpt,
                            "original_offset": span["offset"], "original_length": len(span["text"]), "excerpt": True}
        return None

    def search(self, project_id, query, top_k=5, snapshot_id=None, version_ids=None, section_name=None,
               reading_levels=None, context_blocks=0, plan_id=None, max_chars=None, record_progress=True, clip_hits=False):
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
                spans = [e for e in self.store.find("evidence", parse_artifact_id=item["parse_artifact_id"])
                         if allowed_pages is None or e["pdf_page"] in allowed_pages]
                spans.sort(key=lambda e: (e["pdf_page"], e["offset"], e["id"]))
                page_counts = {}
                for index, span in enumerate(spans):
                    page_index = page_counts.get(span["pdf_page"], 0)
                    page_counts[span["pdf_page"]] = page_index + 1
                    value = score(span["text"])
                    if value:
                        neighbors = [e for e in spans[max(0, index-context_blocks):index+context_blocks+1]
                                     if e["pdf_page"] == span["pdf_page"]]
                        scored.append((value, span["id"], span, neighbors, page_index))
            elif not section_name:
                text = item["bibliography"].get("abstract") or item["bibliography"]["title"]
                value = score(text)
                if value:
                    abstract_results.append({"version_id": item["version_id"], "observation_id": item["observation_id"],
                        "reading_level": item["reading_level"], "score": value,
                        "text": protected(text, "abstract" if item["bibliography"].get("abstract") else "metadata", item["metadata_rights"]),
                        "_raw_length": len(text), "_rights": item["metadata_rights"]})
        scored.sort(key=lambda s: (-s[0], s[1]))
        budget = min(self.settings.max_read_chars if max_chars is None else max_chars, self.settings.max_read_chars)
        selected, seen, matches, truncated = [], set(), 0, False
        context_truncated, hit_truncated, insufficient, budget_versions = False, False, [], set()
        for _, hit_id, hit, neighbors, index in scored:
            if hit_id in seen:
                continue
            if matches == top_k:
                truncated = True
                break
            candidate = self.merged([*selected, hit])
            if not selected and clip_hits and len(hit["text"]) > budget:
                clipped = self.excerpt(hit, budget, (exact, words, grams))
                if clipped:
                    hit = clipped
                    candidate = [hit]
                    hit_truncated = truncated = True
            if sum(len(s["text"]) for s in candidate) > budget:
                truncated = True
                budget_versions.add(hit["version_id"])
                insufficient.append({"evidence_id": hit_id, "required_chars": len(hit["text"]),
                    "read_arguments": {"version_id": hit["version_id"], "parse_artifact_id": hit["parse_artifact_id"],
                        "asset_id": hit["asset_id"], "pages": [hit["pdf_page"]],
                        "evidence_offset": index}})
                continue
            selected.append(hit)
            seen.add(hit_id)
            matches += 1
            for neighbor in sorted(neighbors, key=lambda s: (abs(s["offset"] - hit["offset"]), s["offset"])):
                if neighbor["id"] in seen:
                    continue
                candidate = self.merged([*selected, neighbor])
                if sum(len(s["text"]) for s in candidate) <= budget:
                    selected.append(neighbor)
                    seen.add(neighbor["id"])
                else:
                    context_truncated = truncated = True
        merged = self.merged(selected)
        used = sum(len(s["text"]) for s in merged)
        abstracts = []
        for result in sorted(abstract_results, key=lambda r: -r["score"]):
            if matches >= top_k or used + result["_raw_length"] > budget:
                truncated = True
                if used + result["_raw_length"] > budget:
                    budget_versions.add(result["version_id"])
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
        covered = {s["version_id"] for s in merged if self.gate.can_output("fulltext", s["rights"])} | {
            r["version_id"] for r in abstracts if isinstance(self.gate.output(r["text"]), str)}
        hit_versions = {s[2]["version_id"] for s in scored} | {r["version_id"] for r in abstract_results}
        selected_versions = {s["version_id"] for s in merged} | {r["version_id"] for r in abstracts}
        statuses = {i["version_id"]: ("returned" if i["version_id"] in covered else "policy_withheld"
            if i["version_id"] in selected_versions else "budget_insufficient" if i["version_id"] in budget_versions
            else "result_limit" if i["version_id"] in hit_versions
            else "no_keyword_hit") for i in items}
        return {"snapshot_id": snapshot["id"], "results": self.present_spans(merged) + abstracts,
            "unavailable_pdf_pages": {i["version_id"]: i["extraction_failed_pages"] for i in items
                                      if i.get("extraction_failed_pages")},
            "method": "literal_words_chinese_bigrams_plan_synonyms_v1", "semantic_search": "not_enabled",
            "truncated": truncated, "context_truncated": context_truncated, "hit_truncated": hit_truncated, "selected_chars": used,
            "returned_chars": sum(len(s["text"]) for s in merged if self.gate.can_output("fulltext", s["rights"]))
                + sum(len(self.gate.output(r["text"])) for r in abstracts if isinstance(self.gate.output(r["text"]), str)),
            "budget_insufficient": insufficient, "version_status": statuses,
            "uncovered_version_ids": [i["version_id"] for i in items if i["version_id"] not in covered],
            "coverage_certified": False}
