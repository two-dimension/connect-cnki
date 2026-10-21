from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4
MIGRATIONS = {
    1: (
        "CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX records_kind ON records(kind)",
        "CREATE TABLE project_papers (project_id TEXT NOT NULL REFERENCES records(id), "
        "version_id TEXT NOT NULL REFERENCES records(id), PRIMARY KEY(project_id,version_id))",
        "CREATE TABLE audit (request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, "
        "status TEXT NOT NULL, error_code TEXT, at TEXT NOT NULL)",
    ),
    2: (
        "ALTER TABLE audit ADD COLUMN elapsed_ms REAL",
        "ALTER TABLE audit ADD COLUMN stage TEXT NOT NULL DEFAULT 'tool'",
        "ALTER TABLE audit ADD COLUMN job_id TEXT",
        "ALTER TABLE audit ADD COLUMN source_kind TEXT",
        "CREATE INDEX audit_job_id ON audit(job_id)",
    ),
    3: (
        "CREATE INDEX records_project ON records(kind,json_extract(body,'$.project_id'))",
        "CREATE TABLE review_dependencies (project_id TEXT NOT NULL REFERENCES records(id), "
        "dependent_id TEXT NOT NULL REFERENCES records(id), source_id TEXT NOT NULL REFERENCES records(id), "
        "relation TEXT NOT NULL, PRIMARY KEY(dependent_id,source_id,relation))",
        "CREATE INDEX review_dependency_source ON review_dependencies(project_id,source_id)",
    ),
    4: (
        "CREATE INDEX records_version ON records(kind,json_extract(body,'$.version_id'))",
        "CREATE INDEX records_parse ON records(kind,json_extract(body,'$.parse_artifact_id'))",
        "CREATE INDEX records_asset ON records(kind,json_extract(body,'$.asset_id'))",
        "CREATE INDEX records_idempotency ON records(kind,json_extract(body,'$.idempotency_key'))",
        "ALTER TABLE audit ADD COLUMN diagnostic_code TEXT",
    ),
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AppError(Exception):
    def __init__(self, code: str, message: str, action: str = "", retryable: bool = False,
                 diagnostic_code: str | None = None):
        self.code, self.message, self.action, self.retryable = code, message, action, retryable
        self.diagnostic_code = diagnostic_code
        super().__init__(message)


def require(condition: Any, code: str, message: str, action: str = "") -> None:
    if not condition:
        raise AppError(code, message, action)


def classify_error(error: Exception) -> AppError:
    from pydantic import ValidationError

    if isinstance(error, AppError):
        return error
    if isinstance(error, (ValidationError, json.JSONDecodeError, UnicodeError)):
        return AppError("INVALID_ARGUMENT", "参数或文件内容不符合要求。", "检查输入格式与编码。")
    if isinstance(error, PermissionError):
        return AppError("FILE_ACCESS_DENIED", "没有文件读写权限。", "检查目录和文件访问权限后重试。")
    if isinstance(error, FileNotFoundError):
        return AppError("FILE_NOT_FOUND", "所需文件不存在。", "检查文件和依赖安装路径。")
    if isinstance(error, TimeoutError):
        return AppError("OPERATION_TIMEOUT", "操作超时。", "稍后重试或检查超时配置。", retryable=True)
    if isinstance(error, OSError):
        return AppError("FILE_IO_ERROR", "文件读写失败。", "检查磁盘空间和文件可用性。", retryable=True)
    if isinstance(error, ImportError):
        return AppError("DEPENDENCY_UNAVAILABLE", "所需依赖未安装。", "按安装说明修复本地依赖。")
    return AppError("INTERNAL_ERROR", "内部处理失败，请根据请求或任务标识检查本地诊断。")


def diagnostic_category(error: Exception) -> str:
    allowed = {"AppError", "ValidationError", "JSONDecodeError", "UnicodeDecodeError", "PermissionError",
               "FileNotFoundError", "OSError", "TimeoutError", "ModuleNotFoundError", "ImportError",
               "KeyError", "TypeError", "ValueError", "RuntimeError", "PdfReadError", "PdfStreamError",
               "DependencyError", "FileNotDecryptedError", "WrongPasswordError", "AttributeError", "OtherPdfError", "LimitReachedError"}
    category = error.diagnostic_code if isinstance(error, AppError) and error.diagnostic_code else type(error).__name__
    return category if category in allowed else "OtherError"


class Store:
    """Versioned SQLite record store with explicit project/version relationships.

    Entity payloads retain immutable observation and policy snapshots. Relationships are
    indexed separately so work merges never rewrite citation, asset or evidence identities.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        for folder in ("assets", "parses", "exports", "tmp", "backups"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self._lock_handle = (self.root / "service.lock").open("a+b")
        try:
            import sys

            if sys.platform == "win32":
                import msvcrt

                if self._lock_handle.tell() == 0:
                    self._lock_handle.write(b"0")
                    self._lock_handle.flush()
                self._lock_handle.seek(0)
                msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_handle.close()
            raise AppError("DATA_DIR_IN_USE", "该数据目录已有服务运行。", "复用现有连接或为另一实例配置独立数据目录。") from None
        self.db = None
        try:
            self.db = sqlite3.connect(self.root / "library.sqlite3", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            require(version <= SCHEMA_VERSION, "SCHEMA_TOO_NEW", "数据库来自更新版本，请升级程序。")
            self._migrate(version)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.recover_interrupted()
        except BaseException:
            if self.db is not None:
                self.db.close()
            self._lock_handle.close()
            raise

    def _migrate(self, version: int):
        if version == SCHEMA_VERSION:
            return
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            self._backup_before_migration(version)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    self.db.execute(statement)
                self.db.execute(f"PRAGMA user_version={target}")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _backup_before_migration(self, version: int):
        name = f"schema-{version}-to-{SCHEMA_VERSION}-{uuid.uuid4().hex}"
        snapshot = self.root / "backups" / f"{name}.sqlite3"
        destination = sqlite3.connect(snapshot)
        try:
            # SQLite's backup API includes committed WAL pages in a consistent snapshot.
            self.db.backup(destination)
        finally:
            destination.close()
        with snapshot.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"database": snapshot.name, "sha256": checksum, "created_at": now(),
                    "from_version": version, "to_version": SCHEMA_VERSION,
                    "purpose": "pre_migration_snapshot",
                    "asset_policy": "immutable assets and parses remain in place; references are in the database"}
        snapshot.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def audit(self, request_id: str, operation: str, status: str, error_code: str | None,
              elapsed_ms: float, *, stage: str = "tool", job_id: str | None = None, source_kind: str = "local",
              diagnostic_code: str | None = None):
        # Fixed field set: no arguments, query strings, exceptions, credentials or text bodies.
        self.db.execute(
            "INSERT INTO audit (request_id,operation,status,error_code,at,elapsed_ms,stage,job_id,source_kind,diagnostic_code) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request_id, operation, status, error_code, now(), max(0, round(elapsed_ms, 3)), stage, job_id,
             source_kind, diagnostic_code),
        )

    def close(self):
        self.db.close()
        self._lock_handle.close()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def put(self, kind: str, body: dict, record_id: str | None = None) -> dict:
        record_id = record_id or body.get("id") or uid(kind)
        value = {**body, "id": record_id}
        self.db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "body=excluded.body,updated_at=excluded.updated_at",
            (record_id, kind, json.dumps(value, ensure_ascii=False), now(), now()),
        )
        return value

    def get(self, record_id: str, kind: str | None = None) -> dict:
        row = self.db.execute("SELECT kind,body FROM records WHERE id=?", (record_id,)).fetchone()
        require(row and (kind is None or row["kind"] == kind), "NOT_FOUND", "本地标识不存在或类型不符。")
        return json.loads(row["body"])

    def all(self, kind: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM records WHERE kind=? ORDER BY created_at,rowid", (kind,)
        )]

    def find(self, kind: str, **filters) -> list[dict]:
        allowed = {"version_id", "parse_artifact_id", "asset_id", "idempotency_key", "project_id"}
        if not filters or not set(filters) <= allowed:
            raise ValueError("Unsupported record filter")
        clauses = [f"json_extract(body,'$.{field}')=?" for field in filters]
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM records WHERE kind=? AND " + " AND ".join(clauses) + " ORDER BY created_at,rowid",
            (kind, *filters.values()))]

    def paper_count(self, project_id: str) -> int:
        return self.db.execute("SELECT count(*) FROM project_papers WHERE project_id=?", (project_id,)).fetchone()[0]

    def has_paper(self, project_id: str, version_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM project_papers WHERE project_id=? AND version_id=?",
                               (project_id, version_id)).fetchone() is not None

    def link(self, project_id: str, version_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO project_papers VALUES (?,?)", (project_id, version_id))

    def papers(self, project_id: str) -> list[dict]:
        self.get(project_id, "project")
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT r.body FROM project_papers p JOIN records r ON r.id=p.version_id "
            "WHERE p.project_id=? ORDER BY r.created_at,r.rowid", (project_id,)
        )]

    def recover_interrupted(self) -> None:
        with self.db:
            for job in self.all("job"):
                if job["state"] in ("queued", "running"):
                    job.update(state="waiting_for_user", recovery_reason="process_restarted")
                    self.put("job", job)
            for session in self.all("session"):
                if session["state"] != "closed":
                    session.update(state="expired", error_code="BROWSER_SESSION_UNAVAILABLE")
                    self.put("session", session)


def normalize(text: str) -> str:
    import unicodedata

    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())
