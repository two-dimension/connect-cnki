from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

from .core import AppError, require


class _PdfWarnings(logging.Handler):
    def __init__(self):
        super().__init__()
        self.duplicates = 0
        self.other = 0

    def emit(self, record):
        if record.getMessage().startswith("Multiple definitions in dictionary"):
            self.duplicates += 1
        else:
            self.other += 1


@contextmanager
def _capture_warnings():
    # Provider object names, offsets and content never enter the MCP diagnostics.
    logger = logging.getLogger("pypdf")
    previous = (logger.handlers[:], logger.level, logger.propagate)
    captured = _PdfWarnings()
    logger.handlers = [captured]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield captured
    finally:
        logger.handlers, level, logger.propagate = previous
        logger.setLevel(level)


def inspect_pdf(source: Path, max_pages: int) -> tuple[PdfReader, int, str, int]:
    def read(strict):
        reader = PdfReader(source, strict=strict)
        try:
            # PdfReader can open standard empty-user-password documents by default;
            # is_encrypted stays true even when the reader already has access.
            # Never supply, guess or retain passwords, or rewrite the source file.
            pages = len(reader.pages)
        except FileNotDecryptedError:
            raise AppError("FILE_VALIDATION_FAILED", "PDF 需要打开密码，未尝试密码输入。",
                           "请先使用正常阅读器合法打开并提供可直接读取的副本。") from None
        require(0 < pages <= max_pages, "FILE_VALIDATION_FAILED", "PDF 页数超限或为空。")
        for page in reader.pages:
            _ = page.mediabox
            if not strict:
                contents = page.get_contents()
                if contents is not None:
                    contents.get_data()
        return reader, pages

    with _capture_warnings() as warnings:
        try:
            reader, pages = read(True)
            if warnings.other or warnings.duplicates:
                raise AppError("FILE_VALIDATION_FAILED", "PDF 存在尚未支持的结构警告。",
                               diagnostic_code="PdfStructureWarning")
            return reader, pages, "pdf_structural", 0
        except PdfReadError as error:
            if not str(error).startswith("Multiple definitions in dictionary"):
                raise
        # Only this observed producer defect receives a compatibility path.
        # pypdf's best-effort behavior is documented at /user/robustness.html.
        with source.open("rb") as stream:
            stream.seek(max(0, source.stat().st_size - 2048))
            tail = stream.read()
        require(b"%%EOF" in tail and b"startxref" in tail, "FILE_VALIDATION_FAILED",
                "PDF 缺少完整结束结构，不能兼容读取。")
        warnings.duplicates = warnings.other = 0
        reader, pages = read(False)
        require(not warnings.other and warnings.duplicates > 0, "FILE_VALIDATION_FAILED",
                "PDF 兼容读取出现额外结构警告。")
        return reader, pages, "pdf_structural_review_required", warnings.duplicates
