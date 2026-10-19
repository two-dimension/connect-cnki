from __future__ import annotations

from difflib import SequenceMatcher

from .config import public_url
from .core import Store, normalize, now, require, uid
from .models import PaperInput
from .policy import protected


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    import re

    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    require(bool(re.fullmatch(r"10\.\d{4,9}/\S+", value)), "INVALID_DOI", "DOI 格式无效。")
    return value


def compatible(a: dict, b: dict) -> bool:
    # Missing fields cannot silently establish identity; conflicts always stop an auto-match.
    return (
        normalize(a["title"]) == normalize(b["title"])
        and bool(a["authors"] and b["authors"])
        and [normalize(x) for x in a["authors"]] == [normalize(x) for x in b["authors"]]
        and a["year"] == b["year"]
        and a["version_type"] == b["version_type"]
        and a["paper_type"] == b["paper_type"]
        and all(not a.get(k) or not b.get(k) or normalize(str(a[k])) == normalize(str(b[k]))
                for k in ("source", "volume", "issue", "pages", "doi"))
    )


class Library:
    def __init__(self, store: Store):
        self.store = store

    def ingest(self, project_id: str, record: PaperInput, rights: dict, origin: str) -> dict:
        paper = record.model_dump()
        paper["doi"] = normalize_doi(paper["doi"])
        paper["detail_url"] = public_url(paper["detail_url"])
        existing = self.store.all("version")
        match, candidates = None, []
        for old in existing:
            same_source = bool(paper["source_id"] and paper["source_provider"] != "import"
                               and old["source_id"] == paper["source_id"]
                               and old["source_provider"] == paper["source_provider"])
            same_doi = bool(paper["doi"] and old["doi"] == paper["doi"])
            if (same_source or same_doi) and compatible(paper, old):
                match = old
                break
            similarity = SequenceMatcher(None, normalize(paper["title"]), normalize(old["title"])).ratio()
            if same_source or same_doi or similarity >= 0.88:
                candidates.append((old, "identifier_conflict" if same_source or same_doi else "similar_title"))
        if match:
            version = match
            if same_source:
                version["detail_url"] = paper["detail_url"]
                if normalize(paper["title"]) == normalize(version["title"]):
                    version["title"] = paper["title"]
                self.store.put("version", version)
        else:
            work = self.store.put("work", {})
            version = self.store.put("version", {**paper, "work_id": work["id"], "rights": rights,
                                                  "observed_at": now(), "source_origin": origin})
        observation = self.store.put("observation", {
            "version_id": version["id"], "fields": paper, "rights": rights,
            "source": origin, "observed_at": now(), "verification": "source_observed",
        })
        self.store.link(project_id, version["id"])
        duplicate_ids = []
        for old, reason in candidates if not match else []:
            duplicate = self.store.put("duplicate", {
                "left_id": old["id"], "right_id": version["id"], "reason": reason, "state": "pending",
            })
            duplicate_ids.append(duplicate["id"])
        return {"version_id": version["id"], "observation_id": observation["id"],
                "matched_existing": bool(match), "duplicate_ids": duplicate_ids}

    def present(self, paper: dict) -> dict:
        metadata = {key: value for key, value in paper.items()
                    if key not in {"abstract", "rights", "source_origin"}}
        metadata["abstract"] = protected(paper.get("abstract"), "abstract", paper["rights"])
        metadata["missing_fields"] = [k for k in ("authors", "year", "source", "doi", "volume", "issue", "pages")
                                      if not paper.get(k)]
        metadata["retraction_status"] = "confirmed" if paper.get("retracted") else "not_checked"
        return metadata

    def resolve(self, duplicate_id: str, relation: str, reason: str) -> dict:
        duplicate = self.store.get(duplicate_id, "duplicate")
        require(duplicate["state"] == "pending", "ALREADY_RESOLVED", "该候选已经处理。")
        left = self.store.get(duplicate["left_id"], "version")
        right = self.store.get(duplicate["right_id"], "version")
        require(relation in {"same_work", "same_version", "extends", "correction_of", "retraction_of", "unrelated"},
                "INVALID_ARGUMENT", "不支持该关系。")
        # Even an explicit same-version decision retains both immutable citation identities.
        mappings = []
        if relation in {"same_work", "same_version"} and left["work_id"] != right["work_id"]:
            for version in self.store.all("version"):
                if version["work_id"] == right["work_id"]:
                    mappings.append({"version_id": version["id"], "before": version["work_id"], "after": left["work_id"]})
                    self.store.put("version", {**version, "work_id": left["work_id"]})
        merge = self.store.put("merge", {
            "duplicate_id": duplicate_id, "relation": relation, "reason": reason,
            "mappings": mappings, "rolled_back": False, "at": now(),
            "left_id": left["id"], "right_id": right["id"],
        }, uid("merge"))
        self.store.put("duplicate", {**duplicate, "state": "resolved", "merge_id": merge["id"]})
        return {"merge_id": merge["id"], "relation": relation,
                "citation_identities_preserved": True, "mappings": mappings}

    def rollback(self, merge_id: str, reason: str) -> dict:
        merge = self.store.get(merge_id, "merge")
        if merge["rolled_back"]:
            return {"merge_id": merge_id, "rolled_back": True}
        # Later changes to the same mapping must be undone first.
        for newer in self.store.all("merge"):
            if newer["id"] != merge_id and not newer["rolled_back"] and newer["at"] >= merge["at"]:
                affected = {x["version_id"] for x in merge["mappings"]}
                require(not affected.intersection(x["version_id"] for x in newer["mappings"]),
                        "MERGE_CONFLICT", "先回滚后续依赖合并。")
        for mapping in merge["mappings"]:
            version = self.store.get(mapping["version_id"], "version")
            require(version["work_id"] == mapping["after"], "MERGE_CONFLICT", "映射已变化，请先处理后续合并。")
            self.store.put("version", {**version, "work_id": mapping["before"]})
        self.store.put("merge", {**merge, "rolled_back": True, "rollback_reason": reason, "rollback_at": now()})
        duplicate = self.store.get(merge["duplicate_id"], "duplicate")
        self.store.put("duplicate", {**duplicate, "state": "pending"})
        return {"merge_id": merge_id, "rolled_back": True}
