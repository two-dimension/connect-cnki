"""Persistent official-MCP client for a bounded, auditable local experiment.

Input: one JSON {tool, arguments} per line, or `file <absolute JSON path>`.
Writes policy-filtered structuredContent into ignored runtime storage. It never
reads the school's login form, browser cookies, or private browser profiles.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cnki_mcp.app import TOOL_NAMES
from cnki_mcp.config import Settings
from cnki_mcp.core import now


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(args):
    config = args.config.resolve(strict=True)
    settings = Settings.load(str(config))
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    # Dedicated experiment library; production code and user's existing projects stay untouched.
    trial_config = runtime / "config.json"
    trial_config.write_text(settings.model_copy(update={"data_dir": runtime / "library"}).model_dump_json(indent=2), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "cnki_mcp", "--config", str(trial_config)], cwd=str(config.parent),
        env={**os.environ, "PYTHONUTF8": "1"})
    sequence = len(list(runtime.glob("response-*.json")))
    if args.queue_dir:
        args.queue_dir.mkdir(parents=True, exist_ok=True)
        (runtime / "bridge-process.json").write_text(json.dumps({"pid": os.getpid(), "started_at": now(),
            "queue_dir": str(args.queue_dir.resolve())}), encoding="utf-8")

    async def next_command():
        if not args.queue_dir:
            return (await asyncio.to_thread(input)).strip(), None
        while True:
            pending = sorted(args.queue_dir.glob("*.request.json"))
            if pending:
                source = pending[0]
                active = source.with_suffix(".processing")
                source.replace(active)
                return "file " + str(active), active
            await asyncio.sleep(.5)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            emit({"bridge": "ready", "runtime": str(runtime), "at": now(), "transport": "official MCP stdio"})
            while True:
                try:
                    line, queue_file = await next_command()
                except EOFError:
                    break
                if line in {"quit", "exit"}:
                    break
                if not line:
                    continue
                try:
                    payload = json.loads(Path(line[5:]).read_text(encoding="utf-8")) if line.startswith("file ") else json.loads(line)
                    batch = payload if isinstance(payload, list) else [payload]
                    for command in batch:
                        tool = command["tool"]
                        if tool == "__shutdown__":
                            if queue_file:
                                queue_file.replace(queue_file.with_suffix(".done"))
                            return
                        if tool not in TOOL_NAMES:
                            raise ValueError("unknown tool")
                        started = time.perf_counter()
                        result = await client.call_tool(tool, command.get("arguments", {}))
                        sequence += 1
                        file = runtime / f"response-{sequence:04d}.json"
                        data = result.structuredContent
                        envelope = {"tool": tool, "at": now(), "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                                    "arguments": command.get("arguments", {}), "result": data, "protocol_error": result.isError,
                                    "request_file": queue_file.name if queue_file else None}
                        file.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
                        summary = {"response": str(file), "tool": tool, "status": data.get("status") if data else "protocol_error"}
                        if data:
                            if data.get("error"):
                                summary["error"] = data["error"]
                            if data.get("job_id"):
                                summary["job_id"] = data["job_id"]
                            value = data.get("data") or {}
                            summary.update({k: value[k] for k in ("id", "project_id", "state", "institution_name", "query_id", "collected", "stop_reason", "next_cursor") if k in value})
                            if "papers" in value:
                                summary["papers"] = len(value["papers"])
                        emit(summary)
                        if not data or data["status"] in {"failed", "waiting_for_user"}:
                            break
                    if queue_file:
                        queue_file.replace(queue_file.with_suffix(".done"))
                except (ValueError, KeyError, TypeError, OSError):
                    emit({"error": "INVALID_BRIDGE_COMMAND"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--queue-dir", type=Path, help="Durable file queue for a hidden background client; no browser credentials persisted.")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except KeyboardInterrupt:
        pass
