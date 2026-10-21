from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from anyio import CancelScope
from pypdf import __version__ as pypdf_version

from . import parsing_identity
from .config import Settings
from .core import AppError, Store, now, require, uid
from .pdf_validation import inspect_pdf
from .policy import PolicyGate, protected
from .processes import run_process


class Documents:
    def __init__(self, settings: Settings, store: Store, gate: PolicyGate):
        self.settings, self.store, self.gate = settings, store, gate

    async def register_async(self, version_id: str, file: Path, rights: dict, source: str) -> dict:
        self.store.get(version_id, "version")
        require(8 <= file.stat().st_size <= self.settings.max_file_mb * 1024 * 1024,
                "FILE_VALIDATION_FAILED", "文件大小不合法或超限。")
        staged = self.store.root / "tmp" / uid("inspection")
        copy = asyncio.create_task(asyncio.to_thread(shutil.copyfile, file, staged))
        try:
            # The copy thread has no Store access. Finish it before cleaning up on cancellation.
            try:
                await asyncio.shield(copy)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await copy
                raise
            inspection = None
            with staged.open("rb") as stream:
                signature = stream.read(16)
            if signature.startswith(b"%PDF-"):
                code, output = await run_process(
                    [sys.executable, "-m", "cnki_mcp.pdf_inspector", str(staged), str(self.settings.max_pdf_pages)],
                    timeout=self.settings.inspect_timeout_seconds, timeout_code="PDF_INSPECTION_TIMEOUT")
                require(code == 0, "FILE_VALIDATION_FAILED", "PDF 检查进程失败。")
                inspection = json.loads(output)
                if "error" in inspection:
                    raise AppError(**inspection["error"])
            return self.register(version_id, staged, rights, source, inspection=inspection)
        finally:
            staged.unlink(missing_ok=True)

    def register(self, version_id: str, file: Path, rights: dict, source: str, *, inspection: dict | None = None) -> dict:
        self.store.get(version_id, "version")
        size = file.stat().st_size
        require(8 <= size <= self.settings.max_file_mb * 1024 * 1024,
                "FILE_VALIDATION_FAILED", "文件大小不合法或超限。")
        with file.open("rb") as stream:
            signature = stream.read(16)
        kind = "pdf" if signature.startswith(b"%PDF-") else "caj" if signature.startswith(b"CAJ") else None
        require(kind, "FILE_VALIDATION_FAILED", "仅接收具有正确文件签名的 PDF 或 CAJ。")
        pages = None
        validation = "signature_only_manual_review"
        pdf_warnings = 0
        pdf_encryption = None
        if kind == "pdf":
            try:
                if inspection is None:
                    reader, pages, validation, pdf_warnings = inspect_pdf(file, self.settings.max_pdf_pages)
                    pdf_encryption = "default_reader_access" if reader.is_encrypted else "none"
                else:
                    pages, validation, pdf_warnings, pdf_encryption = (
                        inspection[k] for k in ("pages", "validation", "warnings", "encryption"))
            except AppError:
                raise
            except (OSError, ImportError):
                raise
            except Exception as error:
                category = type(error).__name__
                if category not in {"PdfReadError", "PdfStreamError", "DependencyError", "FileNotDecryptedError",
                                    "WrongPasswordError", "ValueError", "TypeError", "KeyError", "AttributeError"}:
                    category = "OtherPdfError"
                raise AppError("FILE_VALIDATION_FAILED", "PDF 结构损坏或文件不完整。",
                               diagnostic_code=category) from None
        else:
            # CAJ has no bundled structural validator: preserve it, never report validated fulltext.
            require(source == "user_import", "FILE_VALIDATION_FAILED", "自动下载的 CAJ 尚无完整性检查器，请人工导入。")
        with file.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        target = self.store.root / "assets" / f"{sha}.{kind}"
        if target.exists():
            with target.open("rb") as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == sha,
                        "ASSET_INTEGRITY_FAILED", "已有资产哈希不符。")
        else:
            temporary = self.store.root / "tmp" / uid("asset")
            try:
                shutil.copyfile(file, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        for asset in self.store.find("asset", version_id=version_id):
            if asset["sha256"] == sha and asset["rights"] == rights:
                return self.present_asset(asset)
        asset = self.store.put("asset", {
            "version_id": version_id, "sha256": sha, "format": kind,
            "relative_path": str(target.relative_to(self.store.root)), "size": size, "pages": pages,
            "rights": rights, "source": source, "acquired_at": now(),
            "validation": validation, "pdf_structure_warning_count": pdf_warnings, "pdf_encryption": pdf_encryption,
        })
        return self.present_asset(asset)

    @staticmethod
    def present_asset(asset: dict):
        return {key: value for key, value in asset.items() if key not in {"rights", "relative_path"}}

    def parse_allowed(self, asset: dict):
        rights = asset["rights"]
        if rights.get("origin") == "import":
            require(rights.get("allow_local_parse"), "POLICY_UNVERIFIED", "文件尚未登记本地解析许可。")
        else:
            self.gate.check(rights["institution_id"], "local_parse_index", rights.get("resource_type"))

    async def parse(self, job: dict) -> dict:
        asset = self.store.get(job["arguments"]["asset_id"], "asset")
        self.parse_allowed(asset)
        require(asset["format"] == "pdf", "PARSER_NOT_SUPPORTED", "CAJ 暂不解析，请提供合法 PDF。")
        profile = job["arguments"]["parser_profile"]
        require(profile == "text_pdf", "CAPABILITY_UNAVAILABLE", "OCR/MinerU 尚未启用，请使用 text_pdf 或人工处理。")
        require(job["arguments"].get("pipeline_version", parsing_identity.PIPELINE_VERSION) == parsing_identity.PIPELINE_VERSION,
                "PARSER_VERSION_CHANGED", "解析流程已更新，请重新提交 parse_fulltext 生成新任务。")
        parse_id = uid("parse")
        output = self.store.root / "tmp" / f"{parse_id}.json"
        source = self.store.root / asset["relative_path"]
        with source.open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"],
                    "ASSET_INTEGRITY_FAILED", "原始文件已被修改。")
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-m", "cnki_mcp.pdf_worker", str(source), str(output), str(self.settings.max_pdf_pages),
            "--report-page-failures",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                try:
                    process = await spawn
                except Exception:
                    pass
                else:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
            output.unlink(missing_ok=True)
            raise
        try:
            await asyncio.wait_for(process.wait(), self.settings.parse_timeout_seconds)
            result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
            if process.returncode != 0 or not result:
                raise AppError("PARSING_FAILED", "PDF 解析失败或超限。",
                               diagnostic_code=result.get("error", {}).get("diagnostic_code", "OtherPdfError"))
            require(result.get("pipeline_version") == parsing_identity.PIPELINE_VERSION,
                    "PARSER_VERSION_CHANGED", "解析进程与服务的流程版本不一致，请重启服务。")
        except (TimeoutError, asyncio.CancelledError) as error:
            with CancelScope(shield=True):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise AppError("PARSING_TIMEOUT", "解析超时，原文件已保留。", "检查文件后恢复任务。") from None
        finally:
            output.unlink(missing_ok=True)
        recovered = result["pdf_validation"] == "pdf_structural_review_required"
        extraction_failures = result.get("extraction_failures", [])
        failed_pages = [f["pdf_page"] for f in extraction_failures]
        quality = ("ocr_required" if result["scanned_pages"] else "structure_review_required" if recovered
                   else "layout_review_required" if result["complex_pages"] or failed_pages else "text_extracted_unreviewed")
        artifact = {
            "id": parse_id, "asset_id": asset["id"], "version_id": asset["version_id"], "sha256": asset["sha256"],
            "parser": "pypdf", "parser_version": pypdf_version, "profile": profile, "at": now(),
            "pipeline_version": result.get("pipeline_version", "legacy_text_pdf_v1"),
            "quality": quality, "scanned_pages": result["scanned_pages"], "complex_pages": result["complex_pages"],
            "page_count": len(result["pages"]),
            "pdf_validation": result["pdf_validation"], "pdf_structure_warning_count": result["pdf_structure_warning_count"],
            "pdf_encryption": result.get("pdf_encryption", "unknown"),
            "extraction_failed_pages": failed_pages, "extraction_failures": extraction_failures,
        }
        with self.store.transaction():
            self.store.put("parse", artifact)
            for page in result["pages"]:
                text = page.pop("text")
                for start in range(0, len(text), 1000):
                    self.store.put("evidence", {
                        **page, "text": text[start:start + 1200], "offset": start,
                        "version_id": asset["version_id"], "asset_id": asset["id"],
                        "parse_artifact_id": parse_id, "sha256": asset["sha256"], "rights": asset["rights"],
                        "block_id": f"p{page['pdf_page']}-{start}",
                    })
        manifest = self.store.root / "parses" / f"{parse_id}.json"
        manifest.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"parse_artifact_id": parse_id, "asset_id": asset["id"], "sha256": asset["sha256"],
                "quality": quality, "page_count": len(result["pages"]),
                "reading_level": "fulltext" if any(e["text"].strip() for e in self.store.find("evidence", parse_artifact_id=parse_id)) else "metadata_only",
                "pdf_validation": result["pdf_validation"],
                "pipeline_version": result.get("pipeline_version", "legacy_text_pdf_v1"),
                "extraction_failed_pages": failed_pages, "extraction_failures": extraction_failures,
                "_job_state": "partially_succeeded" if result["scanned_pages"] or recovered or failed_pages else "succeeded",
                "limitations": ["页码为 PDF 页序号；印刷页码、表格、公式和阅读顺序尚未人工核验。"]
                               + (["源 PDF 含重复字典键，已兼容读取；结构与页面解释需要人工复核。"] if recovered else [])
                               + (["部分页触及内容流安全上限，未生成该页证据；见 extraction_failed_pages，需人工查看原页。"] if failed_pages else [])}

    def span(self, evidence: dict):
        return {key: protected(value, "fulltext", evidence["rights"]) if key == "text" else value
                for key, value in evidence.items() if key != "rights"}
