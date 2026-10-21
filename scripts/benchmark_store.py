"""Synthetic scale comparison against the original all-records Python filter."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from cnki_mcp.core import SCHEMA_VERSION, Store, now
from cnki_mcp.jobs import Jobs
from cnki_mcp.processes import run_process


def worker():
    allocation = bytearray(4 * 1024 * 1024)
    started = time.monotonic()
    time.sleep(.1)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        assert query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        peak_rss = counters.PeakWorkingSetSize
    else:
        import resource

        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    assert len(allocation) == 4 * 1024 * 1024
    return {"start": started, "end": time.monotonic(), "peak_rss_bytes": peak_rss}


def measure(call, samples=12):
    times = []
    for _ in range(samples):
        started = time.perf_counter()
        result = call()
        times.append((time.perf_counter() - started) * 1000)
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {"samples": samples, "median_ms": round(statistics.median(times), 3),
                    "p95_ms": round(sorted(times)[math.ceil(samples * .95) - 1], 3), "peak_python_bytes": peak}


async def queue_measurement(store, limit):
    jobs = Jobs(store, {"parse_fulltext": limit})
    active, peak = 0, 0
    intervals = []

    async def handler(job):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            code, output = await run_process([sys.executable, str(Path(__file__).resolve()), "--worker"], timeout=10)
            assert code == 0
            intervals.append(json.loads(output))
            return {"synthetic": True}
        finally:
            active -= 1

    jobs.handlers["parse_fulltext"] = handler
    tracemalloc.start()
    started = time.perf_counter()
    for index in range(12):
        jobs.submit("parse_fulltext", {"index": index}, f"queue-{limit}-{index}")
    await asyncio.gather(*list(jobs.tasks.values()))
    elapsed = (time.perf_counter() - started) * 1000
    _, memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    await jobs.shutdown()
    assert peak <= limit
    events = [(r["start"], 1, r["peak_rss_bytes"]) for r in intervals] + [(r["end"], -1, -r["peak_rss_bytes"]) for r in intervals]
    concurrent, concurrent_peak, rss, rss_bound = 0, 0, 0, 0
    for _, change, memory_change in sorted(events):
        concurrent += change
        rss += memory_change
        concurrent_peak, rss_bound = max(concurrent_peak, concurrent), max(rss_bound, rss)
    assert concurrent_peak <= limit
    return {"limit": limit, "tasks": 12, "peak_active": peak, "elapsed_ms": round(elapsed, 3),
            "peak_parent_python_bytes": memory, "peak_worker_intervals": concurrent_peak,
            "sum_worker_peak_rss_upper_bound_bytes": rss_bound,
            "workload": "12_python_children_each_allocate_4MiB_and_wait_100ms"}


def benchmark(sizes):
    rows = []
    with tempfile.TemporaryDirectory(prefix="cnki-store-benchmark-") as temporary:
        store = Store(Path(temporary))
        try:
            for count in sizes:
                with store.transaction():
                    store.db.execute("DELETE FROM records WHERE kind='evidence'")
                    for index in range(count):
                        store.put("evidence", {"version_id": f"version_{index // 10}",
                            "parse_artifact_id": f"parse_{index // 10}", "asset_id": f"asset_{index // 10}",
                            "offset": index % 10 * 1000, "text": "Synthetic benchmark text. " * 48})
                target = f"parse_{count // 20}"
                def legacy(target=target):
                    return [e for e in store.all("evidence") if e["parse_artifact_id"] == target]

                def indexed(target=target):
                    return store.find("evidence", parse_artifact_id=target)
                old_result, old = measure(legacy)
                new_result, new = measure(indexed)
                assert old_result == new_result
                plan = [tuple(r) for r in store.db.execute(
                    "EXPLAIN QUERY PLAN SELECT body FROM records WHERE kind=? AND json_extract(body,'$.parse_artifact_id')=?",
                    ("evidence", target))]
                rows.append({"evidence_records": count, "matching_records": len(new_result),
                    "legacy_reference": old, "indexed": new, "results_identical": True, "sql_queries_per_lookup": 1,
                    "rows_loaded_before": count, "rows_loaded_after": len(new_result), "query_plan": plan})
            queues = [asyncio.run(queue_measurement(store, limit)) for limit in (1, 2, 4)]
        finally:
            store.close()
    return {"at": now(), "schema": SCHEMA_VERSION, "python": platform.python_version(),
            "platform": platform.platform(), "scope": "isolated_synthetic_only", "store": rows, "queue": queues,
            "limitations": ["Linear reference and indexed lookup share the current schema/runtime; this is not a historical executable benchmark.",
                            "Query Python allocation peaks exclude SQLite/native memory; worker RSS sums use per-process peaks during overlapping workload intervals, not simultaneous OS sampling.",
                            "Synthetic workers measure concurrency enforcement, not an optimal PDF concurrency setting."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[500, 5000, 20000])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    report = json.dumps(worker() if args.worker else benchmark(args.sizes), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)
