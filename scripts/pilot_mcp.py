"""Interactive real-MCP pilot, keeping one dedicated context through human verification."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(arguments):
    configuration = arguments.config.resolve(strict=True)
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "cnki_mcp", "--config", str(configuration)], cwd=str(configuration.parent))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, values):
                result = await client.call_tool(name, values)
                data = result.structuredContent
                if data is None:
                    emit({"tool": name, "protocol_error": result.isError})
                    return {"status": "failed", "data": None}
                emit({"tool": name, "result": data})
                return data

            session_id = None
            try:
                started = await call("start_access_session", {"institution_id": arguments.institution, "mode": arguments.mode})
                if started["status"] == "failed":
                    return
                session_id = started["data"]["id"]
                if arguments.project_id:
                    project_id = arguments.project_id
                else:
                    project = await call("create_project", {"question": f"[真实联调] {arguments.query}",
                        "institution_id": arguments.institution, "budget": arguments.budget})
                    if project["status"] != "succeeded":
                        return
                    project_id = project["data"]["project_id"]
                cursor = None
                last_version_id = None
                print("同一 MCP 会话已保留。命令：verify / search / next / paper / detail / inspect / status / close。", flush=True)
                while True:
                    try:
                        command = (await asyncio.to_thread(input, "pilot> ")).strip().lower()
                    except EOFError:
                        break
                    if command in {"close", "quit", "exit"}:
                        break
                    if command in {"verify", ""}:
                        await call("verify_access_session", {"session_id": session_id})
                    elif command in {"inspect", "status"}:
                        await call("get_access_status", {"session_id": session_id, "operation": "automated_search",
                                                         "inspect_page": command == "inspect"})
                    elif command in {"paper", "detail"}:
                        if not last_version_id:
                            emit({"code": "NO_SELECTED_PAPER", "message": "请先取得检索结果。"})
                            continue
                        await call("get_paper", {"version_id": last_version_id, "refresh": command == "detail",
                                                 "session_id": session_id if command == "detail" else None})
                    elif command in {"search", "next"}:
                        if command == "next" and not cursor:
                            emit({"code": "NO_NEXT_CURSOR", "message": "没有可继续的游标。"})
                            continue
                        values = {"project_id": project_id, "session_id": session_id,
                            "query": {"text": arguments.query, "field": arguments.field}, "limit": arguments.limit}
                        if command == "next":
                            values["cursor"] = cursor
                        result = await call("search_papers", values)
                        cursor = (result.get("data") or {}).get("next_cursor")
                        papers = (result.get("data") or {}).get("papers", [])
                        if papers:
                            last_version_id = papers[0].get("version_id") or papers[0].get("id")
                    else:
                        print("可用命令：verify / search / next / paper / detail / inspect / status / close。", flush=True)
            finally:
                if session_id:
                    await call("close_access_session", {"session_id": session_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--institution", default="xmu")
    parser.add_argument("--project-id", help="复用现有联调项目，避免每次重新创建。")
    parser.add_argument("--mode", choices=["institution_network", "carsi"], default="institution_network")
    parser.add_argument("--query", required=True)
    parser.add_argument("--field", choices=["subject", "title", "keywords", "author", "source"], default="subject")
    parser.add_argument("--limit", type=int, choices=range(1, 21), default=3)
    parser.add_argument("--budget", type=int, default=20)
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        pass
