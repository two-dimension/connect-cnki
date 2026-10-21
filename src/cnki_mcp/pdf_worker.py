"""Bounded subprocess PDF parsing; stdout is never part of the MCP transport."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pypdf import errors as pdf_errors

from .core import diagnostic_category
from .parsing_identity import PIPELINE_VERSION
from .pdf_validation import inspect_pdf

# Older supported readers do not expose this newer guard exception. An empty
# exception tuple catches nothing and preserves their normal failure behavior.
STREAM_LIMIT_ERROR = getattr(pdf_errors, "LimitReachedError", ())


def parse(source: Path, destination: Path, max_pages: int, *, allow_partial_page_failures=False):
    reader, _, validation, structure_warnings = inspect_pdf(source, max_pages)
    pages, scanned, complex_pages, extraction_failures = [], [], [], []
    for index, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text(extraction_mode="plain") or ""
        except STREAM_LIMIT_ERROR as error:
            # Discard the entire failed content stream; never retry it with a
            # larger security budget. Other independent pages keep their limits.
            if not allow_partial_page_failures or str(error) != "Reached maximum number of tokens per stream.":
                raise
            text = ""
            extraction_failures.append({"pdf_page": index, "code": "content_stream_token_limit"})
        failed = bool(extraction_failures and extraction_failures[-1]["pdf_page"] == index)
        if not failed and len(text.strip()) < 30:
            scanned.append(index)
        # Conservatively flag formula/layout hints; this is not a layout-quality certification.
        if failed or any(sign in text for sign in ("∑", "∫", "矩阵", "Table ", "表1", "表 1")):
            complex_pages.append(index)
        pages.append({"pdf_page": index, "printed_page": None, "text": text,
                      "section": None, "bbox": None})
    destination.write_text(json.dumps({"pages": pages, "scanned_pages": scanned,
                                       "complex_pages": complex_pages, "pdf_validation": validation,
                                       "pdf_encryption": "default_reader_access" if reader.is_encrypted else "none",
                                       "extraction_failures": extraction_failures,
                                       "pipeline_version": PIPELINE_VERSION,
                                       "pdf_structure_warning_count": structure_warnings}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        parse(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]),
              allow_partial_page_failures=sys.argv[4:] == ["--report-page-failures"])
    except Exception as error:
        Path(sys.argv[2]).write_text(json.dumps({"error": {"diagnostic_code": diagnostic_category(error),
            "stage": "pdf_extraction"}}), encoding="utf-8")
        sys.exit(2)
