"""Measure status latency while cancellable, controlled import/export workers run."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

from pypdf import PdfWriter

from cnki_mcp import citations, parsing, processes
from cnki_mcp.app import Application
from cnki_mcp.config import Settings
from cnki_mcp.core import now
from cnki_mcp.models import ImportRights, PaperInput


async def benchmark():
    reports = []
    with tempfile.TemporaryDirectory(prefix="cnki-responsive-") as temporary:
        root = Path(temporary)
        app = Application(Settings(data_dir=root / "library", import_roots=[root]))
        original_spawn = asyncio.create_subprocess_exec
        original_inspect, original_render = parsing.run_process, citations.run_process
        try:
            project = (await app.create_project("Synthetic response timing"))["data"]["project_id"]
            version = (await app.import_records(project, [PaperInput(title="Synthetic response timing paper")]))["data"]["imported"][0]["version_id"]

            async def finished(job):
                return {"fixture": True}

            app.jobs.handlers["fixture"] = finished
            job = app.jobs.submit("fixture", {}, "responsiveness-fixture")
            await app.jobs.tasks[job["id"]]
            source = root / "fixture.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=600, height=800)
            writer.write(source)
            for operation in ("pdf_inspection", "csl_render"):
                children, started = [], asyncio.Event()

                async def spawn(*args, children=children, started=started, **kwargs):
                    child = await original_spawn(*args, **kwargs)
                    children.append(child)
                    started.set()
                    return child

                async def controlled(*args, **kwargs):
                    return await processes.run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30)

                processes.asyncio.create_subprocess_exec = spawn
                parsing.run_process = citations.run_process = controlled
                work = asyncio.create_task(app.import_fulltext(version, str(source), ImportRights())
                                           if operation == "pdf_inspection" else app.export_bibliography([version], "text"))
                await asyncio.wait_for(started.wait(), 5)
                times = []
                try:
                    for _ in range(40):
                        begin = time.perf_counter()
                        response = await app.get_job(job["id"])
                        assert response["status"] == "succeeded" and response["data"]["state"] == "succeeded"
                        times.append((time.perf_counter() - begin) * 1000)
                        await asyncio.sleep(0)
                finally:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                assert all(child.returncode is not None for child in children)
                assert not app.store.all("asset") and not app.store.all("export")
                assert not list((app.store.root / "tmp").iterdir())
                reports.append({"operation": operation, "status_query": "get_job", "samples": len(times),
                    "median_ms": round(statistics.median(times), 3), "p95_ms": round(sorted(times)[math.ceil(.95 * len(times)) - 1], 3),
                    "max_ms": round(max(times), 3), "cancelled_workers_reaped": True, "temporary_files_cleaned": True})
        finally:
            processes.asyncio.create_subprocess_exec = original_spawn
            parsing.run_process, citations.run_process = original_inspect, original_render
            await app.shutdown()
    return {"at": now(), "scope": "application_tool_functions_with_controlled_30s_children",
            "measurements": reports, "limitations": "Excludes stdio transport and real PDF/CSL workload; no production latency SLO inferred."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = json.dumps(asyncio.run(benchmark()), ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.write_text(report + "\n", encoding="utf-8")
    print(report)
