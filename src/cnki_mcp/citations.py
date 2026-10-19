from __future__ import annotations

import io
import json
import re
from pathlib import Path

import bibtexparser
import rispy
from bibtexparser.bibdatabase import BibDatabase
from citeproc import Citation, CitationItem, CitationStylesBibliography, CitationStylesStyle, formatter
from citeproc.source.json import CiteProcJSON

from .core import AppError, digest, now, require
from .models import PaperInput

TYPES = {"journal": "article-journal", "conference": "paper-conference", "thesis": "thesis", "book": "book",
         "preprint": "article", "correction": "article-journal", "retraction": "article-journal", "other": "article"}
RIS_TYPES = {"journal": "JOUR", "conference": "CONF", "thesis": "THES", "book": "BOOK"}


def csl_record(paper: dict) -> dict:
    item = {"id": paper["id"], "type": TYPES[paper["paper_type"]], "title": paper["title"]}
    if paper["authors"]:
        # Preserve source author strings; do not guess Chinese/Western family-name boundaries.
        item["author"] = [{"literal": author} for author in paper["authors"]]
    if paper["year"]:
        item["issued"] = {"date-parts": [[paper["year"]]]}
    for key, target in {"source": "container-title", "volume": "volume", "issue": "issue",
                        "pages": "page", "doi": "DOI", "detail_url": "URL"}.items():
        if paper.get(key):
            item[target] = paper[key]
    return item


def serialize(papers: list[dict], format: str, style_path: Path | None = None):
    csl = [csl_record(paper) for paper in papers]
    if format == "csl-json":
        return json.dumps(csl, ensure_ascii=False, indent=2), "json"
    if format == "ris":
        entries = []
        for paper in papers:
            entry = {"type_of_reference": RIS_TYPES.get(paper["paper_type"], "GEN"), "title": paper["title"],
                     "authors": paper["authors"], "id": paper["id"]}
            for key, target in {"year": "year", "source": "journal_name", "volume": "volume", "issue": "number",
                                "doi": "doi", "detail_url": "url"}.items():
                if paper.get(key):
                    entry[target] = str(paper[key])
            if paper.get("pages"):
                entry["start_page"] = paper["pages"]
            entries.append(entry)
        return rispy.dumps(entries), "ris"
    if format == "bibtex":
        database = BibDatabase()
        escaped = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}"}
        def escape(value):
            return "".join(escaped.get(c, c) for c in str(value))
        for paper in papers:
            entry = {"ENTRYTYPE": {"journal": "article", "conference": "inproceedings", "book": "book",
                                   "thesis": "phdthesis"}.get(paper["paper_type"], "misc"),
                     "ID": paper["id"], "title": "{" + escape(paper["title"]) + "}"}
            if paper["authors"]:
                entry["author"] = " and ".join("{" + escape(a) + "}" for a in paper["authors"])
            for key, target in {"year": "year", "source": "journal", "volume": "volume", "issue": "number",
                                "pages": "pages", "doi": "doi", "detail_url": "url"}.items():
                if paper.get(key):
                    if key == "source" and paper["paper_type"] == "conference":
                        target = "booktitle"
                    if key == "source" and paper["paper_type"] == "thesis":
                        target = "school"
                    entry[target] = escape(paper[key])
            database.entries.append(entry)
        return bibtexparser.dumps(database), "bib"
    if format == "text":
        require(style_path and style_path.is_file(), "STYLE_UNAVAILABLE", "请求的 CSL 样式尚未安装或核验。")
        style = CitationStylesStyle(str(style_path), validate=False)
        bibliography = CitationStylesBibliography(style, CiteProcJSON(csl), formatter.plain)
        bibliography.register(Citation([CitationItem(item["id"]) for item in csl]))
        return "\n".join(str(entry) for entry in bibliography.bibliography()), "txt"
    raise AppError("INVALID_ARGUMENT", "不支持该导出格式。")


def parse_records(text: str, format: str) -> list[PaperInput]:
    if format == "json":
        values = json.loads(text)
        return [PaperInput.model_validate(v) for v in values]
    if format == "csl-json":
        values = json.loads(text)
        result = []
        for item in values:
            authors = [a.get("literal") or " ".join(filter(None, [a.get("given"), a.get("family")]))
                       for a in item.get("author", [])]
            date = item.get("issued", {}).get("date-parts", [[]])[0]
            result.append(PaperInput(
                title=item["title"], authors=authors, year=date[0] if date else None,
                paper_type=next((k for k, v in TYPES.items() if v == item.get("type")), "other"),
                source=item.get("container-title"), doi=item.get("DOI"), detail_url=item.get("URL"),
                volume=item.get("volume"), issue=item.get("issue"), pages=item.get("page"), abstract=item.get("abstract"),
            ))
        return result
    if format == "ris":
        values = rispy.load(io.StringIO(text))
        return [PaperInput(
            title=v.get("title") or v.get("primary_title"), authors=v.get("authors", []),
            year=int(re.search(r"\d{4}", v["year"])[0]) if v.get("year") else None,
            paper_type=next((k for k, code in RIS_TYPES.items() if code == v.get("type_of_reference")), "other"),
            source=v.get("journal_name") or v.get("secondary_title"), doi=v.get("doi"),
            volume=v.get("volume"), issue=v.get("number"), abstract=v.get("abstract"),
            pages=v.get("start_page"), detail_url=v.get("url"),
        ) for v in values]
    if format == "bibtex":
        database = bibtexparser.loads(text)
        return [PaperInput(
            title=v["title"].strip("{}"), authors=[a.strip("{} ") for a in v.get("author", "").split(" and ") if a],
            year=int(v["year"]) if v.get("year") else None, doi=v.get("doi"),
            paper_type={"article": "journal", "inproceedings": "conference", "book": "book",
                        "phdthesis": "thesis", "mastersthesis": "thesis"}.get(v["ENTRYTYPE"], "other"),
            source=v.get("journal") or v.get("booktitle") or v.get("school"), abstract=v.get("abstract"),
            volume=v.get("volume"), issue=v.get("number"), pages=v.get("pages"), detail_url=v.get("url"),
        ) for v in database.entries]
    raise AppError("INVALID_ARGUMENT", "不支持该导入格式。")


def export_manifest(papers: list[dict], format: str, style: str, style_path: Path | None) -> dict:
    return {"format": format, "style": style, "at": now(), "renderer": "citeproc-py" if format == "text" else "deterministic",
            "style_sha256": digest(style_path.read_text(encoding="utf-8")) if style_path else None,
            "version_ids": [p["id"] for p in papers],
            "missing_fields": {p["id"]: [key for key in ("authors", "year", "source", "volume", "issue", "pages")
                                         if not p.get(key)] for p in papers},
            "validation": "pilot_golden_set_pending"}
