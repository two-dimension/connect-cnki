"""Isolated structural validation. Only bounded metadata is emitted."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .core import AppError, classify_error, diagnostic_category
from .pdf_validation import inspect_pdf


def inspect(source: Path, max_pages: int) -> dict:
    try:
        reader, pages, validation, warnings = inspect_pdf(source, max_pages)
        return {"pages": pages, "validation": validation, "warnings": warnings,
                "encryption": "default_reader_access" if reader.is_encrypted else "none"}
    except Exception as error:
        classified = classify_error(error) if isinstance(error, (AppError, OSError, ImportError)) else AppError(
            "FILE_VALIDATION_FAILED", "PDF 结构损坏或文件不完整。")
        return {"error": {"code": classified.code, "message": classified.message,
                          "action": classified.action, "retryable": classified.retryable,
                          "diagnostic_code": diagnostic_category(error)}}


if __name__ == "__main__":
    print(json.dumps(inspect(Path(sys.argv[1]), int(sys.argv[2])), ensure_ascii=False))
