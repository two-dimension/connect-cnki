from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .core import AppError, require

Operation = Literal["automated_search", "metadata_collect", "fulltext_download", "local_parse_index", "external_model_use"]
Mode = Literal["institution_network", "carsi"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyRecord(StrictModel):
    operation: Operation
    state: Literal["allowed", "denied", "unknown"] = "unknown"
    source: str = ""
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    verified_by: str = ""
    resource_types: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    min_interval_seconds: float = Field(default=5, ge=0.1)
    max_records: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def evidence_required(self):
        if self.state == "allowed":
            require(bool(self.source and self.verified_at and self.expires_at and self.verified_by),
                    "CONFIG_INVALID", "allowed 许可必须提供依据、核验人、核验时间与有效期。")
            require(self.verified_at.tzinfo and self.expires_at.tzinfo,
                    "CONFIG_INVALID", "许可时间必须包含时区。")
            require(bool(self.resource_types), "CONFIG_INVALID", "许可必须明确资源类型范围。")
        return self


class Selectors(StrictModel):
    institution_label: str = ""
    search_input: str = "#txt_SearchText"
    search_button: str = ".search-btn"
    result_rows: str = "table.result-table-list tbody tr"
    title_link: str = "td.name a.fz14"
    authors: str = "td.author"
    source: str = "td.source"
    date: str = "td.date"
    next_page: str = "#PageNext"
    empty_results: str = ".no-result"
    detail_title: str = ".wx-tit h1"
    detail_authors: str = ".author span a"
    detail_source: str = ".top-tip span a"
    abstract: str = "#ChDivSummary"
    keywords: str = ".keywords a"
    pdf_download: str = "#pdfDown"
    not_subscribed: str = ""
    resource_available: str = ""
    sort_controls: dict[Literal["latest", "cited"], str] = Field(default_factory=dict)
    filter_controls: dict[str, str] = Field(default_factory=dict)
    search_fields: dict[str, str] = Field(default_factory=dict)


class Institution(StrictModel):
    institution_id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    display_names: list[str] = Field(min_length=1)
    access_modes: list[Mode] = Field(default_factory=list)
    entry_urls: dict[Mode, str] = Field(default_factory=dict)
    search_url: str = ""
    allowed_hosts: list[str] = Field(default_factory=list)
    provider_hosts: list[str] = Field(default_factory=list)
    resource_hosts: list[str] = Field(default_factory=list)
    selectors: Selectors = Field(default_factory=Selectors)
    policy_records: list[PolicyRecord] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    rules_version: str = "unverified"
    guide_sources: list[str] = Field(default_factory=list)
    acceptance: str = "pending_real_school_verification"

    @model_validator(mode="after")
    def validate_urls(self):
        require(len({p.operation for p in self.policy_records}) == len(self.policy_records),
                "CONFIG_INVALID", "每项操作只允许一条当前许可。")
        require(set(self.provider_hosts) <= set(self.allowed_hosts), "CONFIG_INVALID", "来源主机需在允许列表中。")
        for url in [*self.entry_urls.values(), self.search_url]:
            if url:
                validate_url(url, self.allowed_hosts)
        for mode in self.access_modes:
            require(mode in self.entry_urls, "CONFIG_INVALID", "已启用模式缺少入口。")
        return self


class ClientProfile(StrictModel):
    location: Literal["unknown", "local", "institution", "external"] = "unknown"
    provider: str = ""
    allowed_content: list[Literal["metadata", "abstract", "fulltext", "review"]] = Field(
        default_factory=lambda: ["metadata"]
    )


class Settings(StrictModel):
    data_dir: Path = Field(default_factory=lambda: Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "cnki-mcp")
    import_roots: list[Path] = Field(default_factory=list)
    client: ClientProfile = Field(default_factory=ClientProfile)
    institutions: list[Institution] = Field(default_factory=list)
    max_candidates: int = Field(default=100, ge=1, le=10000)
    max_file_mb: int = Field(default=50, ge=1, le=500)
    max_pdf_pages: int = Field(default=200, ge=1, le=2000)
    max_read_chars: int = Field(default=6000, ge=100, le=30000)
    parse_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    network_timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    browser_headless: bool = False
    browser_channel: Literal["chromium", "msedge", "chrome"] = "chromium"
    csl_styles: dict[str, Path] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_institutions(self):
        ids = [x.institution_id for x in self.institutions]
        require(len(ids) == len(set(ids)), "CONFIG_INVALID", "学校标识不可重复。")
        return self

    @classmethod
    def load(cls, path: str | None = None):
        config_path = path or os.environ.get("CNKI_MCP_CONFIG")
        if not config_path:
            return cls()
        file = Path(config_path).resolve(strict=True)
        data = json.loads(file.read_text(encoding="utf-8"))
        for key in ("data_dir",):
            if key in data:
                data[key] = str((file.parent / data[key]).resolve())
        data["import_roots"] = [str((file.parent / p).resolve()) for p in data.get("import_roots", [])]
        data["csl_styles"] = {k: str((file.parent / p).resolve()) for k, p in data.get("csl_styles", {}).items()}
        return cls.model_validate(data)

    def institution(self, institution_id: str) -> Institution:
        for profile in self.institutions:
            if profile.institution_id == institution_id:
                return profile
        raise AppError("INSTITUTION_UNVERIFIED", "学校档案不存在。", "先配置并核验学校档案。")

    def import_path(self, path: str) -> Path:
        resolved = Path(path).resolve(strict=True)
        require(any(resolved.is_relative_to(root.resolve()) for root in self.import_roots),
                "PATH_NOT_ALLOWED", "文件不在明确配置的导入目录内。")
        require(resolved.is_file(), "PATH_NOT_ALLOWED", "只能导入普通文件。")
        require(resolved.stat().st_size <= self.max_file_mb * 1024 * 1024,
                "FILE_TOO_LARGE", "文件超过配置大小上限。")
        return resolved


def validate_url(url: str, allowed_hosts: list[str]) -> str:
    parts = urlsplit(url)
    require(parts.scheme == "https" and parts.hostname in allowed_hosts and not parts.username
            and not parts.password and parts.port in (None, 443),
            "HOST_NOT_ALLOWED", "地址超出已核验的 HTTPS 主机列表。", "核验学校入口与跳转主机。")
    return url


def provider_https_upgrade(url: str, profile: Institution, method: str) -> str | None:
    """Canonicalize a provider's plain-HTTP GET return without sending it on the wire.

    CNKI can return from its human challenge to the configured KNS search path using
    HTTP. Only that known provider/path is eligible. IdP, non-GET, foreign hosts and
    nonstandard ports remain blocked. Opaque query bytes stay in the browser only.
    """
    try:
        parts = urlsplit(url)
        if (parts.scheme != "http" or parts.hostname not in profile.provider_hosts
                or method != "GET" or parts.port not in (None, 80)
                or parts.username or parts.password):
            return None
        entries = [profile.search_url, profile.entry_urls.get("institution_network", "")]
        for entry in entries:
            configured = urlsplit(entry)
            base = configured.path.rstrip("/") or "/"
            same_path = parts.path.rstrip("/") == base.rstrip("/") or parts.path.startswith(base.rstrip("/") + "/")
            if configured.hostname == parts.hostname and same_path:
                upgraded = urlunsplit(("https", parts.hostname, parts.path, parts.query, parts.fragment))
                return validate_url(upgraded, profile.allowed_hosts)
    except (ValueError, AppError):
        return None
    return None


def public_url(url: str | None) -> str | None:
    """Only retain stable bibliographic identifiers, never callbacks or credentials."""
    if not url:
        return None
    from urllib.parse import parse_qsl, urlencode

    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            return None
        if any(word in parts.path.casefold() for word in ("saml", "shibboleth", "login", "oauth", "callback")):
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                           if k.casefold() in {"dbname", "dbcode", "filename"} and re_safe(v)])
        return urlunsplit(("https", parts.hostname, parts.path, query, ""))
    except ValueError:
        return None


def re_safe(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", value))
