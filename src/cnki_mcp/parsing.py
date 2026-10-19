from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path

from pypdf import PdfReader
from pypdf import __version__ as pypdf_version

from .config import Settings
from .core import AppError, Store, now, require, uid
from .policy import PolicyGate, protected


class Documents:
    def __init__(self, settings: Settings, store: Store, gate: PolicyGate):
        self.settings, self.store, self.gate = settings, store, gate

    def register(self, version_id: str, file: Path, rights: dict, source: str) -> dict:
        self.store.get(version_id, "version")
        size = file.stat().st_size
        require(8 <= size <= self.settings.max_file_mb * 1024 * 1024,
                "FILE_VALIDATION_FAILED", "文件大小不合法或超限。")
        with file.open("rb") as stream:
            signature = stream.read(16)
        kind = "pdf" if signature.startswith(b"%PDF-") else "caj" if signature.startswith(b"CAJ") else None
        require(kind, "FILE_VALIDATION_FAILED", "仅接收具有正确文件签名的 PDF 或 CAJ。")
        pages = None
        if kind == "pdf":
            try:
                reader = PdfReader(file, strict=True)
                require(not reader.is_encrypted, "FILE_VALIDATION_FAILED", "加密 PDF 需先由用户合法解锁。")
                pages = len(reader.pages)
                require(0 < pages <= self.settings.max_pdf_pages, "FILE_VALIDATION_FAILED", "PDF 页数超限或为空。")
                for page in reader.pages:
                    _ = page.mediabox
            except AppError:
                raise
            except Exception:
                raise AppError("FILE_VALIDATION_FAILED", "PDF 结构损坏或文件不完整。") from None
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
            shutil.copyfile(file, temporary)
            temporary.replace(target)
        for asset in self.store.all("asset"):
            if asset["version_id"] == version_id and asset["sha256"] == sha and asset["rights"] == rights:
                return self.present_asset(asset)
        asset = self.store.put("asset", {
            "version_id": version_id, "sha256": sha, "format": kind,
            "relative_path": str(target.relative_to(self.store.root)), "size": size, "pages": pages,
            "rights": rights, "source": source, "acquired_at": now(),
            "validation": "pdf_structural" if kind == "pdf" else "signature_only_manual_review",
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
        parse_id = uid("parse")
        output = self.store.root / "tmp" / f"{parse_id}.json"
        source = self.store.root / asset["relative_path"]
        with source.open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"],
                    "ASSET_INTEGRITY_FAILED", "原始文件已被修改。")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "cnki_mcp.pdf_worker", str(source), str(output), str(self.settings.max_pdf_pages),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), self.settings.parse_timeout_seconds)
            require(process.returncode == 0 and output.exists(), "PARSING_FAILED", "PDF 解析失败或超限。")
            result = json.loads(output.read_text(encoding="utf-8"))
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise AppError("PARSING_TIMEOUT", "解析超时，原文件已保留。", "检查文件后恢复任务。") from None
        finally:
            output.unlink(missing_ok=True)
        quality = "ocr_required" if result["scanned_pages"] else "layout_review_required" if result["complex_pages"] else "text_extracted_unreviewed"
        artifact = {
            "id": parse_id, "asset_id": asset["id"], "version_id": asset["version_id"], "sha256": asset["sha256"],
            "parser": "pypdf", "parser_version": pypdf_version, "profile": profile, "at": now(),
            "quality": quality, "scanned_pages": result["scanned_pages"], "complex_pages": result["complex_pages"],
            "page_count": len(result["pages"]),
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
                "quality": quality, "page_count": len(result["pages"]), "reading_level": "fulltext",
                "limitations": ["页码为 PDF 页序号；印刷页码、表格、公式和阅读顺序尚未人工核验。"]}

    def span(self, evidence: dict):
        return {key: protected(value, "fulltext", evidence["rights"]) if key == "text" else value
                for key, value in evidence.items() if key != "rights"}
