import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from cnki_mcp.app import TOOL_NAMES


async def test_real_stdio_handshake_tools_and_resources(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "cnki_mcp", "--config", str(config)], env={**os.environ, "PYTHONUTF8": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == set(TOOL_NAMES)
            for tool in tools.tools:
                assert tool.description and tool.inputSchema["type"] == "object"
                assert tool.outputSchema["type"] == "object"
            result = await session.call_tool("create_project", {"question": "Protocol fixture"})
            assert not result.isError
            data = result.structuredContent
            assert data["status"] == "succeeded"
            project_id = data["data"]["project_id"]
            result = await session.call_tool("import_records", {"project_id": project_id,
                "records": [{"title": "Protocol fixture paper", "abstract": "PRIVATE_PROTOCOL_SECRET"}]})
            version_id = result.structuredContent["data"]["imported"][0]["version_id"]
            toggled = await session.call_tool("set_review_assistance", {"project_id": project_id, "enabled": True})
            assert toggled.structuredContent["data"]["enabled"]
            plan = await session.call_tool("save_review_plan", {"project_id": project_id, "plan": {"goal": "Protocol V2"}})
            assert plan.structuredContent["status"] == "succeeded"
            snapshot = await session.call_tool("create_corpus_snapshot", {"project_id": project_id,
                "items": [{"version_id": version_id}], "plan_id": plan.structuredContent["data"]["id"]})
            assert snapshot.structuredContent["status"] == "succeeded"
            card = await session.call_tool("save_paper_card", {"project_id": project_id,
                "snapshot_id": snapshot.structuredContent["data"]["id"], "card": {"version_id": version_id,
                "fields": [{"name": "method"}]}})
            assert card.structuredContent["status"] == "succeeded"
            assert "PRIVATE_PROTOCOL_SECRET" not in json.dumps(card.structuredContent)
            progress = await session.call_tool("get_review_progress", {"project_id": project_id})
            assert progress.structuredContent["data"]["artifact_counts"]["paper_card"] == 1
            prompts = await session.list_prompts()
            assert {p.name for p in prompts.prompts} == {"literature_review", "review_section", "review_check"}
            resource = await session.read_resource(f"cnki://papers/{version_id}")
            assert "PRIVATE_PROTOCOL_SECRET" not in resource.contents[0].text
            invalid = await session.call_tool("list_papers", {"project_id": project_id, "limit": 21})
            assert invalid.isError


async def test_stdio_status_remains_responsive_and_cancel_reaps_slow_renderer(tmp_path):
    package = tmp_path / "isolated/cnki_mcp"
    shutil.copytree(Path(__file__).parents[1] / "src/cnki_mcp", package,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    marker = tmp_path / "renderer.pid"
    # Controlled fixture replaces only the renderer in a temporary package copy.
    # Its real process must survive until the client cancellation reaches the server.
    (package / "js/render.cjs").write_text(
        'const fs = require("node:fs"); fs.readFileSync(0, "utf8");\n'
        f'fs.writeFileSync({json.dumps(str(marker))}, String(process.pid));\n'
        'setTimeout(() => process.stdout.write(JSON.stringify({text:"fixture",citations:[],entry_ids:[]})), 30000);\n',
        encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-m", "cnki_mcp", "--config", str(config)],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(package.parent)})
    async with stdio_client(params) as (read, write):
        class ObservedSendStream:
            export_request_id = None

            async def send(self, message):
                request = message.message.root.model_dump()
                if request.get("method") == "tools/call" and request.get("params", {}).get("name") == "export_bibliography":
                    self.export_request_id = request["id"]
                await write.send(message)

            async def aclose(self):
                await write.aclose()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                await self.aclose()

        observed = ObservedSendStream()
        async with ClientSession(read, observed) as session:
            await session.initialize()

            async def call(name, arguments):
                response = (await session.call_tool(name, arguments)).structuredContent
                assert response["status"] == "succeeded", response
                return response["data"]

            project = (await call("create_project", {"question": "Synthetic concurrent protocol"}))["project_id"]
            version = (await call("import_records", {"project_id": project,
                "records": [{"title": "Synthetic concurrent protocol paper"}]}))["imported"][0]["version_id"]
            await call("set_review_assistance", {"project_id": project, "enabled": True})
            snapshot = (await call("create_corpus_snapshot", {"project_id": project, "items": [{"version_id": version}]}))["id"]
            job = await call("prepare_synthesis", {"project_id": project, "snapshot_id": snapshot, "subquestion": "Synthetic"})
            async with asyncio.timeout(5):
                while (await call("get_job", {"job_id": job["id"]}))["state"] not in {"succeeded", "partially_succeeded"}:
                    await asyncio.sleep(.01)
            exporting = asyncio.create_task(call("export_bibliography", {"version_ids": [version], "format": "text"}))
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(.01)
            pid = int(marker.read_text())
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, pid)
                assert handle

                def alive():
                    return kernel.WaitForSingleObject(handle, 0) == 258
            else:
                def alive():
                    try:
                        os.kill(pid, 0)
                        return True
                    except ProcessLookupError:
                        return False
            try:
                status = await asyncio.wait_for(call("get_job", {"job_id": job["id"]}), 1)
                assert status["state"] == "succeeded" and not exporting.done() and alive()
                # Cancelling a local Python Future alone sends no MCP message in
                # this SDK; send the actual protocol cancellation notification.
                await session.send_notification(types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=observed.export_request_id, reason="Synthetic cancellation"))))
                with pytest.raises(McpError):
                    await exporting
                async with asyncio.timeout(3):
                    while alive():
                        await asyncio.sleep(.01)
                assert not list((tmp_path / "data/exports").iterdir())
            finally:
                if sys.platform == "win32":
                    kernel.CloseHandle(handle)
