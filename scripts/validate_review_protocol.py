"""Repeat the V2 engineering smoke test through a real, isolated MCP stdio child.

Uses synthetic records only. No browser, network, model, or main library access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cnki_mcp import __version__
from cnki_mcp.app import TOOL_NAMES
from cnki_mcp.core import now


async def validate():
    with tempfile.TemporaryDirectory(prefix="cnki-v2-stdio-") as temporary:
        root = Path(temporary)
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        settings = json.loads(example.read_text(encoding="utf-8"))
        settings.update(data_dir=str(root / "library"), import_roots=[],
                        client={"location": "local", "allowed_content": ["metadata", "abstract", "fulltext", "review"]})
        config = root / "config.json"
        config.write_text(json.dumps(settings), encoding="utf-8")
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "cnki_mcp", "--config", str(config)], env={**os.environ, "PYTHONUTF8": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "cnki-literature"
                tools = (await session.list_tools()).tools
                assert {t.name for t in tools} == set(TOOL_NAMES)
                assert all(t.inputSchema["type"] == t.outputSchema["type"] == "object" for t in tools)
                prompts = (await session.list_prompts()).prompts
                assert {p.name for p in prompts} == {"literature_review", "review_section", "review_check"}
                resources = (await session.list_resources()).resources
                templates = (await session.list_resource_templates()).resourceTemplates

                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, name
                    result = response.structuredContent
                    assert result["status"] == "succeeded", (name, result.get("error"))
                    return result["data"]

                institutions = await call("list_institutions", {})
                assert "xmu" in json.dumps(institutions)
                project = await call("create_project", {"question": "Synthetic V2 protocol regression"})
                project_id = project["project_id"]
                assert not (await call("get_review_progress", {"project_id": project_id}))["enabled"]
                imported = await call("import_records", {"project_id": project_id,
                    "records": [{"title": "Synthetic protocol paper", "authors": ["Fixture Author"],
                                 "year": 2024, "abstract": "Synthetic protocol abstract, not a research finding."}],
                    "rights": {"basis": "Synthetic fixture authored for this test", "allowed_recipients": ["local"]}})
                version_id = imported["imported"][0]["version_id"]
                await call("set_review_assistance", {"project_id": project_id, "enabled": True})
                plan = await call("save_review_plan", {"project_id": project_id, "plan": {"goal": "Protocol only"}})
                snapshot = await call("create_corpus_snapshot", {"project_id": project_id,
                    "items": [{"version_id": version_id}], "plan_id": plan["id"]})
                card = await call("save_paper_card", {"project_id": project_id, "snapshot_id": snapshot["id"],
                    "card": {"version_id": version_id, "fields": [{"name": "method"}]}})
                job = await call("prepare_synthesis", {"project_id": project_id, "snapshot_id": snapshot["id"],
                    "subquestion": "Synthetic material availability", "plan_id": plan["id"]})
                async with asyncio.timeout(20):
                    while True:
                        completed = await call("get_job", {"job_id": job["id"]})
                        if completed["state"] in {"succeeded", "partially_succeeded", "failed", "waiting_for_user", "cancelled"}:
                            break
                        await asyncio.sleep(0.05)
                assert completed["state"] == "succeeded"
                assert len(completed["result"]["matrix"]) == 1
                progress = await call("get_review_progress", {"project_id": project_id})
                assert progress["artifact_counts"]["paper_card"] == 1 and not progress["understanding_certified"]
                await call("get_review_impact", {"project_id": project_id, "changed_ids": [card["entity_id"]]})
                await call("set_review_assistance", {"project_id": project_id, "enabled": False})
                await call("get_review_artifact", {"project_id": project_id, "artifact_id": card["id"]})
                base = await call("export_project", {"project_id": project_id})
                package = json.loads(Path(base["path"]).read_text(encoding="utf-8"))
                assert not {"matrix", "reviews", "review_artifacts", "document"}.intersection(package)
                await call("export_project", {"project_id": project_id, "include_review": True, "format": "markdown"})
                resource = await session.read_resource(f"cnki://papers/{version_id}")
                assert version_id in resource.contents[0].text
                invalid = await session.call_tool("list_papers", {"project_id": project_id, "limit": 21})
                assert invalid.isError
        return {"at": now(), "transport": "official Python MCP SDK stdio subprocess",
            "service_version": __version__, "tool_count": len(tools), "prompt_count": len(prompts),
            "resource_entries": len(resources) + len(templates), "xmu_profile_read": True,
            "schemas": "all registered tools include input and output schemas",
            "v2_opt_in_plan_snapshot_card_job_progress_history_export": "passed",
            "default_export_excludes_review": True, "main_data_directory_used_by_server": False,
            "network_calls": 0, "scientific_quality_certified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(asyncio.run(validate()), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)
