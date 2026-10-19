"""Small local-only latency measurement; synthetic data, no network or model calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import tempfile
import time
from pathlib import Path

from cnki_mcp.app import Application
from cnki_mcp.config import Institution, Settings
from cnki_mcp.models import PaperInput


async def benchmark():
    with tempfile.TemporaryDirectory(prefix="cnki-mcp-benchmark-") as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        app = Application(Settings(data_dir=root, institutions=[
            Institution(institution_id="fixture", display_names=["Synthetic benchmark fixture"]),
        ]))
        try:
            project = (await app.create_project("Synthetic latency fixture"))["data"]["project_id"]
            imported = await app.import_records(project, [PaperInput(title="Synthetic latency fixture paper")])
            version_id = imported["data"]["imported"][0]["version_id"]
            with app.store.transaction():
                session = app.store.put("session", {"institution_id": "fixture", "state": "not_started"})
            measurements = {}
            for name, call in {
                "local_access_status": lambda: app.get_access_status(session["id"]),
                "local_paper_read": lambda: app.get_paper(version_id),
            }.items():
                times = []
                for _ in range(30):
                    start = time.perf_counter()
                    result = await call()
                    assert result["status"] == "succeeded"
                    times.append((time.perf_counter() - start) * 1000)
                measurements[name] = {"samples": 30, "median_ms": round(statistics.median(times), 3),
                    "p95_ms": round(sorted(times)[math.ceil(len(times) * 0.95) - 1], 3)}
            return {"date": "2026-10-19", "python": platform.python_version(), "platform": platform.platform(),
                    "scope": "local_synthetic_data_only_no_network", "measurements": measurements}
        finally:
            await app.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = json.dumps(asyncio.run(benchmark()), ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.write_text(report + "\n", encoding="utf-8")
    print(report)
