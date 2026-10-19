import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_handshake_tools_and_resources(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "cnki_mcp", "--config", str(config)], env={**os.environ, "PYTHONUTF8": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 29
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
            resource = await session.read_resource(f"cnki://papers/{version_id}")
            assert "PRIVATE_PROTOCOL_SECRET" not in resource.contents[0].text
            invalid = await session.call_tool("list_papers", {"project_id": project_id, "limit": 21})
            assert invalid.isError
