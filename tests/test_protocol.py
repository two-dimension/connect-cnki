import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
