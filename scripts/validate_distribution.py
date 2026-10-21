"""Inspect archives and optionally install the wheel in a fresh locked environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED = {"styles/gbt-7714-2025.csl", "styles/gbt-7714-2015.csl", "styles/SOURCES.json",
            "styles/locales-zh-CN.xml", "styles/locales-en-US.xml", "js/render.cjs",
            "js/package.json", "js/package-lock.json", "pdf_inspector.py", "parsing_identity.py"}


def inspect_distribution(folder: Path):
    wheels, sources = list(folder.glob("*.whl")), list(folder.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, "Build one wheel and one sdist in a clean output directory"
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = archive.namelist()
        metadata = next(n for n in wheel_names if n.endswith(".dist-info/METADATA"))
        dependencies = [s for s in archive.read(metadata).decode().splitlines() if s.startswith("Requires-Dist:")]
        assert not any(s.split(":", 1)[1].strip().lower().startswith(("pytest", "ruff", "reportlab")) for s in dependencies)
        package = Path(__file__).resolve().parents[1] / "src/cnki_mcp"
        source_hashes = {}
        for path in sorted(package.rglob("*")):
            if not path.is_file() or {"node_modules", "__pycache__"}.intersection(path.parts):
                continue
            if path.suffix not in {".py", ".csl", ".xml", ".cjs", ".json"}:
                continue
            relative = path.relative_to(package).as_posix()
            assert archive.read(f"cnki_mcp/{relative}") == path.read_bytes(), f"Wheel differs from working source: {relative}"
            source_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    with tarfile.open(sources[0]) as archive:
        source_names = archive.getnames()
    for required in REQUIRED:
        assert f"cnki_mcp/{required}" in wheel_names, required
        assert any(n.endswith(f"/src/cnki_mcp/{required}") for n in source_names), required
    assert any(n.endswith("/uv.lock") for n in source_names)
    forbidden = {"node_modules", ".venv", "data", "imports", ".auth", ".codex", "__pycache__", "backups"}
    for name in [*wheel_names, *source_names]:
        path = PurePosixPath(name.lower())
        assert not forbidden.intersection(path.parts), name
        assert path.name not in {"config.local.json", ".env", "service.lock", "storage_state.json"}, name
        assert path.suffix not in {".pdf", ".sqlite", ".sqlite3", ".db", ".pyc"}, name
    return wheels[0], {"wheel": wheels[0].name, "sdist": sources[0].name, "required_assets": len(REQUIRED),
                       "archive_checks": "passed", "wheel_matches_package_sources": True,
                       "package_source_sha256": hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest(),
                       "wheel_sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
                       "sdist_sha256": hashlib.sha256(sources[0].read_bytes()).hexdigest()}


def install_check(wheel: Path):
    with tempfile.TemporaryDirectory(prefix="cnki-package-check-") as temporary:
        root = Path(temporary)
        environment = {**os.environ, "PYTHONUTF8": "1"}
        environment.pop("CNKI_MCP_CONFIG", None)
        environment.pop("PYTHONPATH", None)
        requirements = root / "requirements.txt"
        subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", str(requirements)],
                       check=True, capture_output=True, env=environment)
        venv = root / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True)
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), "-r", str(requirements)],
                       check=True, capture_output=True, env=environment)
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel.resolve())],
                       check=True, capture_output=True, env=environment)
        config = root / "config.json"
        config.write_text(json.dumps({"data_dir": str(root / "data")}), encoding="utf-8")
        result = subprocess.run([str(python), "-m", "cnki_mcp", "--config", str(config), "--check"],
                                cwd=root, env=environment, check=True, capture_output=True, text=True, encoding="utf-8")
        assert json.loads(result.stdout)["valid"]
        # The installed child always receives the isolated config; never open the user's default library.
        smoke = """
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def check():
    params = StdioServerParameters(command=sys.executable, args=['-m', 'cnki_mcp', '--config', sys.argv[1]],
                                  env={**os.environ, 'PYTHONUTF8': '1'})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == 'cnki-literature'
            created = await client.call_tool('create_project', {'question': 'Synthetic installed wheel smoke'})
            assert created.structuredContent['status'] == 'succeeded'
            listed = await client.call_tool('list_projects', {})
            assert len(listed.structuredContent['data']['projects']) == 1
asyncio.run(check())
"""
        subprocess.run([str(python), "-c", smoke, str(config)],
                       cwd=root, env=environment, check=True, capture_output=True)


def source_check(folder: Path):
    source, = folder.glob("*.tar.gz")
    with tempfile.TemporaryDirectory(prefix="cnki-source-check-") as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        with tarfile.open(source) as archive:
            archive.extractall(root, filter="data")
        project_file, = root.glob("*/pyproject.toml")
        project = project_file.parent
        environment = {**os.environ, "PYTHONUTF8": "1", "UV_PYTHON": sys.executable}
        for key in ("CNKI_MCP_CONFIG", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
            environment.pop(key, None)

        def run(arguments):
            return subprocess.run(arguments, cwd=project, env=environment, check=True,
                                  capture_output=True, text=True, encoding="utf-8")

        run(["uv", "sync", "--locked"])
        npm = shutil.which("npm")
        assert npm, "Node/npm is required for text bibliography tests"
        run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", "src/cnki_mcp/js"])
        run(["uv", "run", "--frozen", "ruff", "check", "src", "tests", "scripts"])
        tests = run(["uv", "run", "--frozen", "pytest", "-q", "-m", "not browser"])
        run(["uv", "run", "--frozen", "python", "scripts/validate_review_protocol.py"])
        return {"locked_install": "passed", "ruff": "passed", "stdio": "passed",
                "non_browser_tests": tests.stdout.strip().splitlines()[-1]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--source-check", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    wheel, report = inspect_distribution(arguments.dist)
    if arguments.install:
        install_check(wheel)
        report["clean_install"] = "passed"
    if arguments.source_check:
        report["clean_source"] = source_check(arguments.dist)
    if arguments.output:
        arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
