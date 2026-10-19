"""Bounded subprocess PDF parsing; stdout is never part of the MCP transport."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pypdf import PdfReader


def parse(source: Path, destination: Path, max_pages: int):
    reader = PdfReader(source, strict=True)
    if reader.is_encrypted:
        raise ValueError("encrypted")
    if not 0 < len(reader.pages) <= max_pages:
        raise ValueError("page_limit")
    pages, scanned, complex_pages = [], [], []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text(extraction_mode="plain") or ""
        if len(text.strip()) < 30:
            scanned.append(index)
        # Conservatively flag formula/layout hints; this is not a layout-quality certification.
        if any(sign in text for sign in ("∑", "∫", "矩阵", "Table ", "表1", "表 1")):
            complex_pages.append(index)
        pages.append({"pdf_page": index, "printed_page": None, "text": text,
                      "section": None, "bbox": None})
    destination.write_text(json.dumps({"pages": pages, "scanned_pages": scanned,
                                       "complex_pages": complex_pages}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        parse(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
    except Exception:
        sys.exit(2)
