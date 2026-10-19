import hashlib
import json
import sqlite3

import pytest

from cnki_mcp import core
from cnki_mcp.core import AppError, Store


def legacy_database(root):
    root.mkdir()
    database = root / "library.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX records_kind ON records(kind);
        CREATE TABLE project_papers (project_id TEXT NOT NULL REFERENCES records(id),
                                    version_id TEXT NOT NULL REFERENCES records(id), PRIMARY KEY(project_id,version_id));
        CREATE TABLE audit (request_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                            status TEXT NOT NULL, error_code TEXT, at TEXT NOT NULL);
        PRAGMA user_version=1;
    """)
    for kind, record in (
        ("project", {"id": "project_legacy", "question": "Preserve the research"}),
        ("version", {"id": "version_legacy", "title": "Existing citation"}),
        ("job", {"id": "job_legacy", "state": "running"}),
    ):
        connection.execute("INSERT INTO records VALUES (?,?,?,?,?)", (record["id"], kind, json.dumps(record), "old", "old"))
    connection.execute("INSERT INTO project_papers VALUES (?,?)", ("project_legacy", "version_legacy"))
    connection.execute("INSERT INTO audit VALUES (?,?,?,?,?)", ("request_legacy", "import_records", "succeeded", None, "old"))
    connection.commit()
    connection.close()
    return database


def test_version_one_migration_preserves_records_relationships_and_backup(tmp_path):
    root = tmp_path / "library"
    legacy_database(root)
    assets = root / "assets"
    assets.mkdir()
    immutable = assets / "existing.pdf"
    immutable.write_bytes(b"existing immutable asset fixture")
    store = Store(root)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION
    assert store.papers("project_legacy")[0]["id"] == "version_legacy"
    assert store.get("job_legacy")["state"] == "waiting_for_user"
    old_audit = dict(store.db.execute("SELECT * FROM audit WHERE request_id='request_legacy'").fetchone())
    assert old_audit["elapsed_ms"] is None and old_audit["operation"] == "import_records"
    assert immutable.read_bytes() == b"existing immutable asset fixture"
    manifest_file, = (root / "backups").glob("*.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    snapshot = manifest_file.parent / manifest["database"]
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["sha256"]
    with sqlite3.connect(snapshot) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        job = json.loads(backup.execute("SELECT body FROM records WHERE id='job_legacy'").fetchone()[0])
        assert job["state"] == "running"  # Snapshot predates both migration and recovery.
    store.close()
    reopened = Store(root)
    reopened.close()
    assert len(list((root / "backups").glob("*.json"))) == 1  # No backup on ordinary restarts.


def test_failed_migration_rolls_back_and_releases_the_directory(tmp_path, monkeypatch):
    root = tmp_path / "library"
    database = legacy_database(root)
    with monkeypatch.context() as patch:
        patch.setitem(core.MIGRATIONS, 2, ("ALTER TABLE audit ADD COLUMN elapsed_ms REAL", "INVALID MIGRATION SQL"))
        with pytest.raises(sqlite3.DatabaseError):
            Store(root)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "elapsed_ms" not in {row[1] for row in connection.execute("PRAGMA table_info(audit)")}
        assert connection.execute("SELECT count(*) FROM project_papers").fetchone()[0] == 1
    repaired = Store(root)  # No leaked owner lock after the failed constructor.
    repaired.close()


def test_newer_schema_is_rejected_without_modification_or_a_leaked_lock(tmp_path):
    root = tmp_path / "library"
    database = legacy_database(root)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={core.SCHEMA_VERSION + 1}")
    before = database.read_bytes()
    for _ in range(2):
        with pytest.raises(AppError) as failure:
            Store(root)
        assert failure.value.code == "SCHEMA_TOO_NEW"
    assert database.read_bytes() == before
    assert not list((root / "backups").glob("*.json"))


async def test_audit_records_timing_and_job_stage_without_request_bodies(app):
    response = await app.create_project("PRIVATE_RESEARCH_QUESTION")
    row = dict(app.store.db.execute("SELECT * FROM audit WHERE request_id=?", (response["request_id"],)).fetchone())
    assert row["elapsed_ms"] >= 0 and row["stage"] == "tool" and row["source_kind"] == "local"
    assert "PRIVATE_RESEARCH_QUESTION" not in str(row)

    async def handle(job):
        return {"synthetic": True}

    app.jobs.handlers["fixture"] = handle
    job = app.jobs.submit("fixture", {"private": "PRIVATE_JOB_ARGUMENT"}, "timing-test")
    await app.jobs.tasks[job["id"]]
    row = dict(app.store.db.execute("SELECT * FROM audit WHERE job_id=?", (job["id"],)).fetchone())
    assert row["status"] == "succeeded" and row["stage"] == "job" and row["elapsed_ms"] >= 0
    assert "PRIVATE_JOB_ARGUMENT" not in str(row)


def test_schema_two_review_migration_and_failed_three_rollback(tmp_path, monkeypatch):
    root = tmp_path / "v2"
    database = legacy_database(root)
    with sqlite3.connect(database) as connection:
        for statement in core.MIGRATIONS[2]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
    with monkeypatch.context() as patch:
        patch.setitem(core.MIGRATIONS, 3, (core.MIGRATIONS[3][0], "INVALID MIGRATION SQL"))
        with pytest.raises(sqlite3.DatabaseError):
            Store(root)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='records_project'").fetchone()
    store = Store(root)
    assert store.db.execute("SELECT count(*) FROM review_dependencies").fetchone()[0] == 0
    assert store.papers("project_legacy")[0]["id"] == "version_legacy"
    store.close()
