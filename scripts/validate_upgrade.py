"""Install the actual pre-improvement wheel, then upgrade an isolated synthetic library.

The final probe changes ONLY the installed pipeline identity in the temporary
environment, checking invalidation without changing Settings or dependencies.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "aab98eeb5d32d06ab732b19ee8a6dbc6b27e543e"
CASE = r'''
import asyncio, hashlib, json, sys
from pathlib import Path
from cnki_mcp import __version__
from cnki_mcp.app import Application
from cnki_mcp.config import ClientProfile, Settings
from cnki_mcp.models import ClaimInput, ImportRights, PaperInput
from cnki_mcp.review_models import CorpusItemInput
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

phase, root = sys.argv[1], Path(sys.argv[2])
imports = root / "imports"
imports.mkdir(exist_ok=True)
state_file = root / "state.json"
settings = Settings(data_dir=root / "library", import_roots=[imports],
    client=ClientProfile(location="local", allowed_content=["metadata", "abstract", "fulltext", "review"]))
def ok(response):
    assert response["status"] == "succeeded", response.get("error")
    return response["data"]
async def run():
    app = Application(settings)
    try:
        if phase == "before":
            assert __version__ == "0.2.0"
            project = ok(await app.create_project("Synthetic installed upgrade"))["project_id"]
            rights = ImportRights(basis="Synthetic upgrade fixture", allow_local_parse=True, allowed_recipients=["local"])
            version = ok(await app.import_records(project, [PaperInput(title="Synthetic upgrade paper",
                authors=["Fixture Author"], year=2024, doi="10.1234/upgrade")], rights=rights))["imported"][0]["version_id"]
            writer = PdfWriter()
            page = writer.add_blank_page(width=600, height=800)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 40 750 Td (Synthetic upgrade evidence stays readable across versions.) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            source = imports / "fixture.pdf"
            writer.write(source)
            asset = ok(await app.import_fulltext(version, str(source), rights))
            job = ok(await app.parse_fulltext(asset["id"]))
            await app.jobs.tasks[job["id"]]
            parsed = ok(await app.get_job(job["id"]))
            assert parsed["state"] == "succeeded", parsed
            read = ok(await app.read_paper(version))
            evidence = read["evidence"][0]
            ok(await app.set_review_assistance(project, True))
            snapshot = ok(await app.create_corpus_snapshot(project, [CorpusItemInput(version_id=version)]))
            review = ok(await app.save_review(project, f"Synthetic upgrade evidence [{version}]", [
                ClaimInput(text="Synthetic upgrade evidence", version_id=version, evidence_ids=[evidence["id"]])]))
            state = {"project": project, "version": version, "asset": asset["id"], "parse": read["parse_artifact_id"],
                     "job": job["id"], "snapshot": snapshot["id"], "review": review["review_id"],
                     "evidence": evidence["id"], "sha256": asset["sha256"]}
            assert app.store.db.execute("PRAGMA user_version").fetchone()[0] == 3
            state_file.write_text(json.dumps(state), encoding="utf-8")
            return {"service_version": __version__, "schema": 3, "fixture_created": True}
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert app.store.db.execute("PRAGMA user_version").fetchone()[0] == 4
        old = ok(await app.read_paper(state["version"]))
        assert old["parse_artifact_id"] == state["parse"]
        frozen = ok(await app.read_paper(state["version"], project_id=state["project"], snapshot_id=state["snapshot"]))
        assert frozen["parse_artifact_id"] == state["parse"]
        assert "Synthetic upgrade evidence" in app.store.get(state["evidence"], "evidence")["text"]
        assert app.store.get(state["review"], "review")["validation"]["identity_valid"]
        asset = app.store.get(state["asset"], "asset")
        assert hashlib.sha256((app.store.root / asset["relative_path"]).read_bytes()).hexdigest() == state["sha256"]
        submitted = ok(await app.parse_fulltext(state["asset"]))
        assert submitted["id"] != state["job"]
        assert ok(await app.parse_fulltext(state["asset"]))["id"] == submitted["id"]
        await app.jobs.tasks[submitted["id"]]
        parsed = ok(await app.get_job(submitted["id"]))
        assert parsed["state"] == "succeeded", parsed
        result = parsed["result"]
        assert result["parse_artifact_id"] != state["parse"]
        if phase == "pipeline_only":
            assert submitted["id"] != state["current_job"]
            assert app.store.get(submitted["id"], "job")["arguments"]["config_digest"] == state["config_digest"]
            assert result["pipeline_version"] == state["pipeline_version"] + ".identity-probe"
            assert result["parse_artifact_id"] != state["current_parse"]
        else:
            state.update(current_job=submitted["id"], current_parse=result["parse_artifact_id"],
                         config_digest=app.store.get(submitted["id"], "job")["arguments"]["config_digest"],
                         pipeline_version=result["pipeline_version"])
            state_file.write_text(json.dumps(state), encoding="utf-8")
        assert ok(await app.read_paper(state["version"]))["parse_artifact_id"] == state["parse"]
        manifests = list((app.store.root / "backups").glob("*.json"))
        assert len(manifests) == 1
        backup = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert backup["from_version"] == 3 and backup["to_version"] == 4
        assert hashlib.sha256((manifests[0].parent / backup["database"]).read_bytes()).hexdigest() == backup["sha256"]
        return {"service_version": __version__, "schema": 4, "new_parse_created": True,
                "old_pdf_evidence_snapshot_review_preserved": True, "same_request_reused": True,
                "backup_verified": True, "pipeline_version": result["pipeline_version"]}
    finally:
        await app.shutdown()
print(json.dumps(asyncio.run(run())))
'''


def validate(wheel: Path, baseline: str):
    environment = {**os.environ, "PYTHONUTF8": "1"}
    for name in ("PYTHONPATH", "CNKI_MCP_CONFIG"):
        environment.pop(name, None)

    def command(args, *, cwd=None):
        return subprocess.run(args, cwd=cwd, env=environment, check=True, capture_output=True)

    revision = command(["git", "rev-parse", f"{baseline}^{{commit}}"]).stdout.decode().strip()
    with tempfile.TemporaryDirectory(prefix="cnki-installed-upgrade-") as temporary:
        root = Path(temporary)
        before = root / "baseline"
        before.mkdir()
        archive = command(["git", "archive", "--format=tar", revision]).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(before, filter="data")
        command(["uv", "build", "--wheel", "--out-dir", str(root / "old-dist")], cwd=before)
        old_wheel, = (root / "old-dist").glob("*.whl")
        requirements = root / "requirements.txt"
        command(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", str(requirements)])
        venv = root / "venv"
        command(["uv", "venv", "--python", sys.executable, str(venv)])
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        command(["uv", "pip", "install", "--python", str(python), "-r", str(requirements)])
        command(["uv", "pip", "install", "--python", str(python), "--no-deps", str(old_wheel)])
        case = root / "case.py"
        case.write_text(CASE, encoding="utf-8")
        old = json.loads(command([str(python), str(case), "before", str(root)], cwd=root).stdout)
        command(["uv", "pip", "install", "--python", str(python), "--no-deps", "--reinstall", str(wheel.resolve())])
        upgraded = json.loads(command([str(python), str(case), "after", str(root)], cwd=root).stdout)
        identity_file = command([str(python), "-c", "import cnki_mcp.parsing_identity as p; print(p.__file__)"]).stdout.decode().strip()
        identity_path = Path(identity_file)
        assert identity_path.resolve().is_relative_to(venv.resolve())
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        source = identity_path.read_text(encoding="utf-8")
        assert source.count(state["pipeline_version"]) == 1
        identity_path.write_text(source.replace(state["pipeline_version"], state["pipeline_version"] + ".identity-probe"), encoding="utf-8")
        pipeline = json.loads(command([str(python), str(case), "pipeline_only", str(root)], cwd=root).stdout)
    return {"baseline_commit": revision, "baseline": old, "upgraded": upgraded, "pipeline_only_probe": pipeline,
            "scope": "installed_wheels_isolated_synthetic_library_no_network_or_real_cnki"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.wheel is None:
        arguments.wheel, = arguments.dist.glob("*.whl")
    report = json.dumps(validate(arguments.wheel, arguments.baseline), ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.write_text(report + "\n", encoding="utf-8")
    print(report)
