from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError
from pypdf import __version__ as pypdf_version

from . import __version__
from .access import BrowserAccess
from .citations import export_manifest, parse_records, serialize
from .config import Mode, Settings, StrictModel
from .core import AppError, Store, digest, normalize, now, require, uid
from .jobs import Jobs
from .library import Library
from .models import (
    ClaimInput,
    DimensionRating,
    ImportRights,
    PaperInput,
    ScreeningRules,
    SearchFilters,
    SearchQuery,
)
from .parsing import Documents
from .policy import PolicyGate, protected
from .providers import CNKIProvider
from .reviews import Reviews
from .screening import Screening

ID = Annotated[str, Field(min_length=1, max_length=100)]
IDS = Annotated[list[ID], Field(min_length=1, max_length=100)]


class Response(StrictModel):
    status: Literal["succeeded", "partially_succeeded", "waiting_for_user", "failed", "cancelled"]
    data: dict[str, Any] | None
    warnings: list[str]
    provenance: dict[str, Any]
    request_id: str
    error: dict[str, Any] | None = None
    next_cursor: str | None = None
    job_id: str | None = None


def endpoint(method):
    # Publish the shared response contract so the SDK supplies structuredContent.
    method.__annotations__["return"] = Response
    @functools.wraps(method)
    async def execute(self, *args, **kwargs):
        request_id = uid("request")
        started = time.perf_counter()
        try:
            data = await method(self, *args, **kwargs)
            self.store.db.commit()
            status = data.pop("_status", "succeeded")
            warnings = data.pop("_warnings", [])
            result = {"status": status, "data": self.gate.output(data), "warnings": warnings,
                      "request_id": request_id, "provenance": {"service_version": __version__,
                      "config_digest": self.config_digest, "at": now(), "model_calls": 0}}
            if data.get("next_cursor"):
                result["next_cursor"] = data["next_cursor"]
            if data.get("operation") and data.get("id", "").startswith("job_"):
                result["job_id"] = data["id"]
        except Exception as error:
            self.store.db.rollback()
            if isinstance(error, (ValidationError, ValueError, KeyError, TypeError, OSError)):
                error = AppError("INVALID_ARGUMENT", "参数或文件内容不符合要求，请检查输入和目录配置。")
            if not isinstance(error, AppError):
                error = AppError("INTERNAL_ERROR", "操作失败，诊断仅记录错误类别。")
            result = {"status": "failed", "data": None, "warnings": [], "provenance": {}, "request_id": request_id,
                      "error": {"code": error.code, "message": error.message, "user_action": error.action,
                                "retryable": error.retryable, "requires_user_action": bool(error.action),
                                "job_id": None, "request_id": request_id}}
        self.store.audit(request_id, method.__name__, result["status"], result.get("error", {}).get("code"),
                         (time.perf_counter() - started) * 1000, job_id=result.get("job_id"),
                         source_kind="cnki" if method.__name__ in {
                             "start_access_session", "verify_access_session", "search_papers", "acquire_fulltext",
                         } or (method.__name__ == "get_paper" and kwargs.get("refresh", len(args) > 1 and args[1]))
                         else "local")
        self.store.db.commit()
        return result
    return execute


class Application:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_digest = digest(settings.model_dump(mode="json"))
        self.store = Store(settings.data_dir)
        self.gate = PolicyGate(settings)
        self.library = Library(self.store)
        self.access = BrowserAccess(settings, self.store)
        self.provider = CNKIProvider(settings, self.access, self.gate)
        self.jobs = Jobs(self.store)
        self.documents = Documents(settings, self.store, self.gate)
        self.screening = Screening(self.store)
        self.reviews = Reviews(self.store, self.library, self.documents)
        self.jobs.handlers = {"parse_fulltext": self.documents.parse, "acquire_fulltext": self._download}
        self.query_locks: dict[str, asyncio.Lock] = {}

    async def shutdown(self):
        await self.jobs.shutdown()
        await self.access.shutdown()
        self.store.close()

    def project_paper(self, project_id: str, version_id: str) -> dict:
        require(version_id in {p["id"] for p in self.store.papers(project_id)},
                "PROJECT_MISMATCH", "文献不属于指定项目。")
        return self.store.get(version_id, "version")

    @asynccontextmanager
    async def online(self, session_id: str, project: dict, run: dict | None = None):
        try:
            async with self.access.locked(session_id) as context:
                profile = await self.access.ready(session_id, project, context)
                yield context, profile
        except AppError as error:
            self.store.db.rollback()
            session = self.store.get(session_id, "session")
            states = {"SESSION_EXPIRED": "expired", "BROWSER_SESSION_UNAVAILABLE": "expired",
                      "USER_INTERACTION_REQUIRED": "waiting_for_user", "AUTHENTICATION_REQUIRED": "waiting_for_user",
                      "HOST_NOT_ALLOWED": "paused", "INSTITUTION_MISMATCH": "paused",
                      "PROVIDER_RATE_LIMITED": "paused", "SOURCE_LAYOUT_CHANGED": "paused",
                      "NETWORK_NOT_READY": "paused"}
            if error.code in states:
                self.store.put("session", {**session, "state": states[error.code], "error_code": error.code})
            if run:
                self.store.put("query", {**run, "cached": [], "stop_reason": error.code})
            self.store.db.commit()
            raise

    @endpoint
    async def create_project(self, question: Annotated[str, Field(min_length=1, max_length=4000)],
                             institution_id: str | None = None, budget: Annotated[int, Field(ge=1, le=10000)] = 100) -> dict:
        """创建本地研究项目；学校可为空以先导入文献，候选预算受本地上限约束。"""
        if institution_id:
            self.settings.institution(institution_id)
        require(budget <= self.settings.max_candidates, "BUDGET_EXCEEDED", "预算超过本地保护上限。")
        project = self.store.put("project", {"question": question, "institution_id": institution_id,
            "budget": budget, "created_at": now(), "parser_profile": "text_pdf", "config_digest": self.config_digest})
        return {"project_id": project["id"], **project}

    @endpoint
    async def list_projects(self) -> dict:
        """列出本地研究项目和候选数量，便于重启后恢复工作。"""
        return {"projects": [{**p, "paper_count": len(self.store.papers(p["id"]))} for p in self.store.all("project")]}

    @endpoint
    async def list_papers(self, project_id: ID, offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=20)] = 20) -> dict:
        """分页读取项目中的本地题录与待核查重复；不会联网或下载全文。"""
        papers = self.store.papers(project_id)
        ids = {p["id"] for p in papers}
        return {"papers": [self.library.present(p) for p in papers[offset:offset + limit]], "total": len(papers),
                "next_offset": offset + limit if offset + limit < len(papers) else None,
                "duplicates": [d for d in self.store.all("duplicate") if d["state"] == "pending"
                               and d["left_id"] in ids and d["right_id"] in ids]}

    @endpoint
    async def save_screening_policy(self, rules: ScreeningRules, name: Annotated[str, Field(max_length=200)] = "default") -> dict:
        """保存不可变筛选规则版本；支持关闭、学科模板、年份、类型及缺失策略。"""
        policy = self.store.put("policy", {"name": name, "rules": rules.model_dump(), "at": now(),
                                           "rules_digest": digest(rules.model_dump())})
        return {"policy_id": policy["id"], **policy}

    @endpoint
    async def review_decision(self, decision_id: ID, result: Literal["recommended", "conditional", "pending_review", "excluded"],
                              reason: Annotated[str, Field(min_length=1, max_length=2000)]) -> dict:
        """追加人工覆盖与理由，保留原筛选结果和历史覆盖记录。"""
        decision = self.store.get(decision_id, "decision")
        paper = self.store.get(decision["version_id"], "version")
        override = self.store.put("override", {"decision_id": decision_id, "result": result, "reason": reason, "at": now()})
        return {**override, "reason": protected(reason, "review", paper["rights"])}

    @endpoint
    async def resolve_duplicate(self, duplicate_id: ID,
                                relation: Literal["same_work", "same_version", "extends", "correction_of", "retraction_of", "unrelated"],
                                reason: Annotated[str, Field(min_length=1, max_length=2000)]) -> dict:
        """确认待核查关系；研究集可合并，但引用版本、文件与页码保持原标识。"""
        return self.library.resolve(duplicate_id, relation, reason)

    @endpoint
    async def rollback_merge(self, merge_id: ID, reason: Annotated[str, Field(min_length=1, max_length=2000)]) -> dict:
        """按保存映射回滚合并；存在后续依赖时返回冲突。"""
        return self.library.rollback(merge_id, reason)

    @endpoint
    async def list_institutions(self, query: Annotated[str, Field(max_length=200)] = "") -> dict:
        """列出学校档案、已启用模式、许可摘要及真实接入验收状态。"""
        return {"institutions": [{"institution_id": p.institution_id, "display_names": p.display_names,
            "access_modes": p.access_modes, "acceptance": p.acceptance, "guide_sources": p.guide_sources,
            "policies": [{"operation": r.operation, "state": r.state, "source": r.source,
                          "basis_type": r.basis_type, "permitted_purpose": r.permitted_purpose,
                          "resource_types": r.resource_types, "providers": r.providers,
                          "expires_at": r.expires_at.isoformat() if r.expires_at else None} for r in p.policy_records]}
            for p in self.settings.institutions if not query or any(query in name for name in p.display_names)]}

    @endpoint
    async def start_access_session(self, institution_id: ID, mode: Mode) -> dict:
        """打开项目专用临时浏览器，等待用户完成网络连接或 CARSI 认证；不代填凭据。"""
        return {**await self.access.start(institution_id, mode), "_status": "waiting_for_user"}

    @endpoint
    async def verify_access_session(self, session_id: ID) -> dict:
        """用户完成认证后，核验来源页面精确机构标签；只返回脱敏结果。"""
        result = await self.access.verify(session_id)
        return {**result, "_status": "succeeded" if result["state"] == "ready" else "waiting_for_user"}

    @endpoint
    async def close_access_session(self, session_id: ID, clear_persisted: bool = True) -> dict:
        """关闭并清除本项目临时会话；不宣称注销学校所有 SSO 会话。"""
        return await self.access.close(session_id, clear_persisted)

    @endpoint
    async def get_access_status(self, session_id: ID, operation: str | None = None, inspect_page: bool = False) -> dict:
        """读取会话与操作许可；可显式读取专用页的脱敏诊断，不触发网络或认证。"""
        session = self.store.get(session_id, "session")
        profile = self.settings.institution(session["institution_id"])
        permission = "not_requested"
        if operation:
            try:
                self.gate.check(profile.institution_id, operation)
                permission = "allowed"
            except AppError as error:
                permission = error.code
        result = {"session": session, "operation": operation, "permission": permission,
                  "subscriptions": profile.subscriptions, "usage_purpose": self.settings.usage_purpose, "checked_live": False}
        if inspect_page:
            result["browser_diagnostics"] = await self.access.inspect(session_id)
        return result

    @endpoint
    async def search_papers(self, project_id: ID, session_id: ID, query: SearchQuery,
                            filters: SearchFilters | None = None,
                            sort: Literal["relevance", "latest", "cited"] = "relevance",
                            cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=20)] = 20) -> dict:
        """获准后在知网受控入口检索并入库；游标绑定项目、会话、条件与配置，不代表穷尽检索。"""
        project = self.store.get(project_id, "project")
        require(project["institution_id"], "INSTITUTION_UNVERIFIED", "项目尚未绑定学校。")
        search_policy = self.gate.check(project["institution_id"], "automated_search")
        metadata_policy = self.gate.check(project["institution_id"], "metadata_collect")
        filters = filters or SearchFilters()
        for policy in (search_policy, metadata_policy):
            if "*" not in policy.resource_types:
                require(filters.paper_types and set(filters.paper_types) <= set(policy.resource_types),
                        "POLICY_SCOPE_REQUIRED", "检索必须显式限制在许可的文献类型内。")
        snapshot = {"project_id": project_id, "session_id": session_id, "query": query.model_dump(),
                    "filters": filters.model_dump(), "sort": sort, "config_digest": self.config_digest}
        cursor_record = None
        if cursor:
            cursor_record = self.store.get(cursor, "cursor")
            run = self.store.get(cursor_record["query_id"], "query")
            require(run["snapshot"] == snapshot, "CURSOR_MISMATCH", "游标不属于当前条件、会话或配置。")
            require(run["stop_reason"] in (None, "page_limit"), "CURSOR_EXPIRED", "查询已终止，请重新检索。")
            require(not cursor_record["consumed"], "CURSOR_CONSUMED", "该游标已使用，请使用响应中的下一游标。")
        else:
            run = self.store.put("query", {"snapshot": snapshot, "pages_read": 0, "collected": 0,
                "at": now(), "stop_reason": None, "cached": [], "has_next": True,
                "entry": self.settings.institution(project["institution_id"]).search_url})
            self.store.db.commit()
        lock = self.query_locks.setdefault(run["id"], asyncio.Lock())
        async with lock:
            run = self.store.get(run["id"], "query")
            if cursor_record:
                require(not self.store.get(cursor, "cursor")["consumed"], "CURSOR_CONSUMED", "游标已使用。")
            candidate_cap = min(project["budget"], search_policy.max_records, metadata_policy.max_records)
            remaining = candidate_cap - run["collected"]
            project_remaining = candidate_cap - len(self.store.papers(project_id))
            require(remaining > 0 and project_remaining > 0, "BUDGET_EXCEEDED", "候选预算已用完。")
            async with self.online(session_id, project, run) as (context, profile):
                if not run["cached"]:
                    records, has_next = await self.provider.page(context, profile, session_id, run["id"], query,
                                                                filters, sort, run["pages_read"] + 1,
                                                                record_limit=min(remaining, project_remaining))
                    run.update(cached=[r.model_dump() for r in records], has_next=has_next,
                               pages_read=run["pages_read"] + 1)
                # Another session or a local import may have filled the project
                # while this request awaited the provider. Recheck before ingestion.
                project_remaining = candidate_cap - len(self.store.papers(project_id))
                require(project_remaining > 0, "BUDGET_EXCEEDED", "候选预算已用完。")
                take = min(limit, remaining, project_remaining)
                selected, rest = run["cached"][:take], run["cached"][take:]
                output = []
                for value in selected:
                    self.gate.check(project["institution_id"], "metadata_collect", value["paper_type"])
                    rights = {"origin": "institution", "institution_id": project["institution_id"],
                              "resource_type": value["paper_type"]}
                    item = self.library.ingest(project_id, PaperInput.model_validate(value), rights, "cnki_search")
                    output.append(self.library.present(self.store.get(item["version_id"], "version")))
                run.update(cached=rest, collected=run["collected"] + len(selected))
                budget_hit = run["collected"] >= candidate_cap or len(self.store.papers(project_id)) >= candidate_cap
                end = not rest and not run["has_next"]
                next_cursor = None
                if not end and not budget_hit:
                    next_cursor = self.store.put("cursor", {"query_id": run["id"], "consumed": False})["id"]
                budget_reason = "permission_budget" if candidate_cap < project["budget"] else "candidate_budget"
                run["stop_reason"] = budget_reason if budget_hit else "source_end" if end else "page_limit"
                run["applied_sort"] = sort
                run["sort_verification"] = "active_control_match" if (
                    profile.selectors.sort_active_class and sort in profile.selectors.sort_controls) else "configured_default"
                self.store.put("query", run)
                if cursor_record:
                    self.store.put("cursor", {**cursor_record, "consumed": True})
                return {"query_id": run["id"], "papers": output, "next_cursor": next_cursor,
                        "pages_read": run["pages_read"], "collected": run["collected"],
                        "effective_candidate_budget": candidate_cap,
                        "applied_sort": sort, "sort_verification": run["sort_verification"],
                        "stop_reason": run["stop_reason"], "exhaustive": False, "fetched_at": now()}

    @endpoint
    async def get_paper(self, version_id: ID, refresh: bool = False, session_id: str | None = None) -> dict:
        """默认读取本地题录；refresh=true 才按指定会话及许可刷新详情并保留新观测。"""
        paper = self.store.get(version_id, "version")
        if refresh:
            require(session_id, "INVALID_ARGUMENT", "刷新必须显式指定会话。")
            session = self.store.get(session_id, "session")
            institution_id = paper["rights"].get("institution_id")
            require(institution_id == session["institution_id"], "INSTITUTION_MISMATCH", "版本来源与会话学校不一致。")
            self.gate.check(institution_id, "metadata_collect", paper["paper_type"])
            async with self.online(session_id, {"institution_id": institution_id}) as (context, profile):
                detail = await self.provider.detail(context, profile, session_id, paper)
                require(normalize(detail["title"]) == normalize(paper["title"]),
                        "SOURCE_IDENTITY_CONFLICT", "详情题名与保存版本冲突，保留原记录。")
                self.store.put("observation", {"version_id": version_id, "fields": detail,
                                               "rights": paper["rights"], "observed_at": now(), "source": "cnki_detail"})
                paper.update(abstract=detail["abstract"], abstract_status=detail.get("abstract_status", "unknown"), observed_at=now())
                self.store.put("version", paper)
        return {"paper": self.library.present(paper),
            "_warnings": ["来源页面当前只显示截断摘要；不能视为完整摘要。"] if paper.get("abstract_status") == "partial" else [],
            "observations": [
            {"id": o["id"], "source": o["source"], "observed_at": o["observed_at"],
             "fields": {k: protected(v, "abstract", o["rights"]) if k == "abstract" else v
                        for k, v in o["fields"].items()}}
            for o in self.store.all("observation") if o["version_id"] == version_id]}

    @endpoint
    async def import_records(self, project_id: ID, records: list[PaperInput] | None = None, file_path: str | None = None,
                             format: Literal["json", "csl-json", "ris", "bibtex"] = "json",
                             rights: ImportRights | None = None) -> dict:
        """从允许目录或结构化内容导入合法取得题录；摘要输出需明确内容接收方授权。"""
        project = self.store.get(project_id, "project")
        require((records is not None) != (file_path is not None), "INVALID_ARGUMENT", "records 与 file_path 需且只能提供一项。")
        if file_path:
            file = self.settings.import_path(file_path)
            records = parse_records(file.read_text(encoding="utf-8-sig"), format)
        require(records and len(records) <= 100, "INVALID_ARGUMENT", "每次导入需为 1 至 100 条题录。")
        require(len(self.store.papers(project_id)) + len(records) <= project["budget"],
                "BUDGET_EXCEEDED", "导入可能超出项目候选预算。")
        access = {"origin": "import", **(rights or ImportRights()).model_dump()}
        return {"imported": [self.library.ingest(project_id, record, access, "user_import") for record in records]}

    @endpoint
    async def screen_papers(self, version_ids: IDS, policy_id: ID,
                            stage: Literal["metadata", "fulltext"] = "metadata",
                            ratings: dict[str, list[DimensionRating]] | None = None) -> dict:
        """执行可解释筛选；全文评分由 Agent 提供并必须绑定本地证据，不自动判定论文真伪。"""
        policy = self.store.get(policy_id, "policy")
        return {"decisions": [self.screening.run(self.store.get(i, "version"), policy, stage, (ratings or {}).get(i, []))
                              for i in version_ids]}

    @endpoint
    async def acquire_fulltext(self, project_id: ID, session_id: ID, version_id: ID,
                               idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]) -> dict:
        """按显式会话排队获准全文下载；许可未知时返回人工获取状态，不点击下载。"""
        paper = self.project_paper(project_id, version_id)
        project = self.store.get(project_id, "project")
        session = self.store.get(session_id, "session")
        require(project["institution_id"] == session["institution_id"], "INSTITUTION_MISMATCH", "会话与项目学校不符。")
        self.gate.check(session["institution_id"], "fulltext_download", paper["paper_type"])
        return self.jobs.submit("acquire_fulltext", {"project_id": project_id, "session_id": session_id,
                                "version_id": version_id, "config_digest": self.config_digest}, idempotency_key)

    async def _download(self, job: dict) -> dict:
        arguments = job["arguments"]
        project = self.store.get(arguments["project_id"], "project")
        paper = self.project_paper(project["id"], arguments["version_id"])
        self.gate.check(project["institution_id"], "fulltext_download", paper["paper_type"])
        rights = {"origin": "institution", "institution_id": project["institution_id"], "resource_type": paper["paper_type"]}
        for asset in self.store.all("asset"):
            if asset["version_id"] == paper["id"] and asset["rights"] == rights and asset["validation"] == "pdf_structural":
                cached_file = self.store.root / asset["relative_path"]
                if cached_file.is_file():
                    with cached_file.open("rb") as stream:
                        require(hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"],
                                "ASSET_INTEGRITY_FAILED", "缓存文件已被修改，请核验原始文件。")
                    return self.documents.present_asset(asset)
        temporary = self.store.root / "tmp" / f"{job['id']}.download"
        try:
            async with self.online(arguments["session_id"], project) as (context, profile):
                await self.provider.download(context, profile, arguments["session_id"], paper, temporary)
                asset = self.documents.register(paper["id"], temporary, rights, "cnki_download")
                self.store.db.commit()
                return asset
        finally:
            temporary.unlink(missing_ok=True)

    @endpoint
    async def import_fulltext(self, version_id: ID, file_path: str, rights: ImportRights) -> dict:
        """从允许目录导入合法取得 PDF/CAJ，校验结构与哈希并关联发表版本。"""
        return self.documents.register(version_id, self.settings.import_path(file_path),
                                       {"origin": "import", **rights.model_dump()}, "user_import")

    @endpoint
    async def parse_fulltext(self, asset_id: ID, parser_profile: Literal["text_pdf", "ocr", "mineru"] = "text_pdf") -> dict:
        """排队解析文本 PDF；OCR、MinerU 未启用时明确降级，保留不可变证据快照。"""
        asset = self.store.get(asset_id, "asset")
        self.documents.parse_allowed(asset)
        require(parser_profile == "text_pdf", "CAPABILITY_UNAVAILABLE", "OCR/MinerU 尚未启用。")
        return self.jobs.submit("parse_fulltext", {"asset_id": asset_id, "parser_profile": parser_profile,
                                "parser_version": pypdf_version, "config_digest": self.config_digest},
                                digest(["parse", asset_id, parser_profile, pypdf_version, self.config_digest]))

    def selected_parse(self, version_id: str, asset_id: str | None, parse_artifact_id: str | None) -> dict:
        if parse_artifact_id:
            artifact = self.store.get(parse_artifact_id, "parse")
            require(artifact["version_id"] == version_id and (not asset_id or artifact["asset_id"] == asset_id),
                    "EVIDENCE_MISMATCH", "解析产物、文件与版本不一致。")
            return artifact
        selections = [s for s in self.store.all("selection") if s["version_id"] == version_id]
        if selections and not asset_id:
            return self.store.get(selections[0]["parse_artifact_id"], "parse")
        options = [p for p in self.store.all("parse") if p["version_id"] == version_id
                   and (not asset_id or p["asset_id"] == asset_id)]
        require(options, "FULLTEXT_NOT_PARSED", "没有可用全文解析，请先导入并解析文件。")
        artifact = options[0]
        if not selections:
            self.store.put("selection", {"version_id": version_id, "asset_id": artifact["asset_id"],
                                         "parse_artifact_id": artifact["id"]})
        return artifact

    @endpoint
    async def read_paper(self, version_id: ID, pages: list[int] | None = None, asset_id: str | None = None,
                         parse_artifact_id: str | None = None) -> dict:
        """读取指定 PDF 页和解析快照的证据；默认锁定首次快照，输出受模型内容策略限制。"""
        artifact = self.selected_parse(version_id, asset_id, parse_artifact_id)
        pages = pages or [1]
        require(len(pages) <= 20 and all(1 <= p <= artifact["page_count"] for p in pages),
                "INVALID_ARGUMENT", "PDF 页序号超范围或请求页数过多。")
        spans = [e for e in self.store.all("evidence") if e["parse_artifact_id"] == artifact["id"] and e["pdf_page"] in pages]
        used, selected = 0, []
        for span in spans:
            if used + len(span["text"]) > self.settings.max_read_chars:
                break
            selected.append(self.documents.span(span))
            used += len(span["text"])
        return {"version_id": version_id, "asset_id": artifact["asset_id"], "sha256": artifact["sha256"],
                "parse_artifact_id": artifact["id"], "quality": artifact["quality"], "evidence": selected,
                "truncated": len(selected) < len(spans), "reading_level": "fulltext" if selected else "metadata_only",
                "needs_ocr_or_manual_review": bool(set(pages).intersection(artifact["scanned_pages"]))}

    @endpoint
    async def search_evidence(self, project_id: ID, query: Annotated[str, Field(min_length=1, max_length=500)],
                              top_k: Annotated[int, Field(ge=1, le=10)] = 5) -> dict:
        """按关键词检索项目全文片段，返回版本、文件哈希、解析快照和 PDF 页序号。"""
        ids = {p["id"] for p in self.store.papers(project_id)}
        terms = [normalize(word) for word in query.split() if normalize(word)]
        scored = []
        for evidence in self.store.all("evidence"):
            if evidence["version_id"] not in ids:
                continue
            text = normalize(evidence["text"])
            score = sum(text.count(word) for word in terms)
            if score:
                scored.append((score, evidence))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        results, used = [], 0
        for score, evidence in scored[:top_k]:
            if used + len(evidence["text"]) > self.settings.max_read_chars:
                break
            results.append({"score": score, **self.documents.span(evidence)})
            used += len(evidence["text"])
        return {"results": results, "method": "normalized_keyword_count", "semantic_search": "not_enabled"}

    @endpoint
    async def prepare_review(self, project_id: ID, question: Annotated[str, Field(max_length=4000)] = "",
                             version_ids: list[str] | None = None) -> dict:
        """准备文献矩阵、证据包与综述结构；由调用方 Agent 撰写，服务不另行调用模型。"""
        return self.reviews.prepare(project_id, question, version_ids)

    @endpoint
    async def save_review(self, project_id: ID, body: Annotated[str, Field(min_length=1, max_length=200000)],
                          claims: Annotated[list[ClaimInput], Field(max_length=1000)]) -> dict:
        """保存综述草稿并检查引用身份、证据绑定及原文引语；语义支持仍需人工或模型复核。"""
        return self.reviews.save(project_id, body, claims)

    @endpoint
    async def export_bibliography(self, version_ids: IDS,
                                  format: Literal["ris", "bibtex", "csl-json", "text"] = "csl-json",
                                  style: Literal["gbt-7714-2025", "gbt-7714-2015"] = "gbt-7714-2025") -> dict:
        """确定性导出 RIS、BibTeX、CSL JSON 或 CSL 格式文本，保留缺失字段与样式版本信息。"""
        require(len(set(version_ids)) == len(version_ids), "INVALID_ARGUMENT", "导出版本列表存在重复。")
        papers = [self.store.get(i, "version") for i in version_ids]
        style_path = self.settings.csl_styles.get(style, Path(__file__).parent / "styles" / f"{style}.csl")
        content, suffix = serialize(papers, format, style_path if format == "text" else None)
        export_id = uid("export")
        output = self.store.root / "exports" / f"{export_id}.{suffix}"
        output.write_text(content, encoding="utf-8")
        manifest = export_manifest(papers, format, style, style_path if format == "text" else None)
        output.with_suffix(output.suffix + ".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.put("export", {"id": export_id, "relative_path": str(output.relative_to(self.store.root)),
                                   "content_class": "metadata", "manifest": manifest})
        return {"export_id": export_id, "path": str(output), "content": content, "manifest": manifest}

    @endpoint
    async def export_project(self, project_id: ID) -> dict:
        """导出检索记录、筛选清单、文献矩阵、草稿及任务记录；受限内容按同一出站策略处理。"""
        self.store.get(project_id, "project")
        papers = self.store.papers(project_id)
        ids = {p["id"] for p in papers}
        decisions = [d for d in self.store.all("decision") if d["version_id"] in ids]
        for decision in decisions:
            decision["dimensions"] = protected(decision["dimensions"], "fulltext",
                                                 self.store.get(decision["version_id"], "version")["rights"])
        package = {"matrix": self.reviews.prepare(project_id, "", None),
                   "search_runs": [{k: v for k, v in q.items() if k != "cached"} for q in self.store.all("query")
                                   if q["snapshot"]["project_id"] == project_id],
                   "screening": decisions, "overrides": [], "reviews": [],
                   "bibliography": [self.library.present(p) for p in papers],
                   "run": {"service_version": __version__, "config_digest": self.config_digest, "at": now(),
                           "model_calls": 0, "jobs": [self.jobs.present(j) for j in self.store.all("job")
                            if j["arguments"].get("project_id") == project_id or j["arguments"].get("asset_id") in {
                                a["id"] for a in self.store.all("asset") if a["version_id"] in ids}]}}
        decision_ids = {d["id"] for d in decisions}
        for override in self.store.all("override"):
            if override["decision_id"] in decision_ids:
                paper = self.store.get(self.store.get(override["decision_id"], "decision")["version_id"], "version")
                package["overrides"].append({**override, "reason": protected(override["reason"], "review", paper["rights"])})
        for review in self.store.all("review"):
            if review["project_id"] == project_id:
                package["reviews"].append({"id": review["id"], "body": self.reviews.protect_review(review["body"], review["rights"]),
                                           "validation": review["validation"]})
        output = self.store.root / "exports" / f"{uid('project')}.json"
        output.write_text(json.dumps(self.gate.output(package), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(output), "content_policy_applied": True}

    @endpoint
    async def sync_zotero(self, version_ids: IDS, collection_id: str | None = None) -> dict:
        """查询 P1 Zotero 同步能力；首版未启用，不改写 Zotero 数据库。"""
        return {"enabled": False, "capability": "zotero_sync", "reason": "P1_not_enabled", "_warnings": ["可先导出 RIS/BibTeX，再手工导入 Zotero。"]}

    @endpoint
    async def get_job(self, job_id: ID) -> dict:
        """读取持久任务进度、完成产物、失败原因或恢复检查点。"""
        return self.jobs.present(self.store.get(job_id, "job"))

    @endpoint
    async def resume_job(self, job_id: ID, session_id: str | None = None) -> dict:
        """显式恢复失败或中断任务；下载重新核验会话与许可，已成功任务不重复执行。"""
        return self.jobs.resume(job_id, session_id)

    @endpoint
    async def cancel_job(self, job_id: ID) -> dict:
        """停止新增工作并取消运行任务，保留已完成产物；重复取消结果确定。"""
        return await self.jobs.cancel(job_id)


TOOL_NAMES = [
    "create_project", "list_projects", "list_papers", "save_screening_policy", "review_decision",
    "resolve_duplicate", "rollback_merge", "list_institutions", "start_access_session", "verify_access_session",
    "close_access_session", "get_access_status", "search_papers", "get_paper", "import_records", "screen_papers",
    "acquire_fulltext", "import_fulltext", "parse_fulltext", "read_paper", "search_evidence", "prepare_review",
    "save_review", "export_bibliography", "export_project", "sync_zotero", "get_job", "resume_job", "cancel_job",
]
