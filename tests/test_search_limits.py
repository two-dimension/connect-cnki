from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from cnki_mcp.config import Institution, PolicyRecord
from cnki_mcp.models import PaperInput, SearchQuery
from cnki_mcp.providers import extract_results


def profile(search_limit=20, metadata_limit=20):
    return Institution(institution_id="fixture", display_names=["Fixture"],
        allowed_hosts=["fixture.example"], provider_hosts=["fixture.example"], policy_records=[
            PolicyRecord(operation=operation, state="allowed", source="Synthetic fixture", verified_by="tests",
                verified_at=datetime.now(UTC) - timedelta(days=1), expires_at=datetime.now(UTC) + timedelta(days=1),
                resource_types=["*"], max_records=limit, min_interval_seconds=interval)
            for operation, limit, interval in [("automated_search", search_limit, 1), ("metadata_collect", metadata_limit, 5)]
        ])


@pytest.mark.parametrize(("search_limit", "metadata_limit"), [(20, 2), (2, 20)])
async def test_both_operation_caps_limit_collection_and_new_queries(app, monkeypatch, search_limit, metadata_limit):
    institution = profile(search_limit, metadata_limit)
    app.settings.institutions.append(institution)
    project = (await app.create_project("Quota fixture", "fixture", budget=10))["data"]["project_id"]
    requested_limits = []

    @asynccontextmanager
    async def online(*args):
        yield object(), institution

    async def page(*args, record_limit=None):
        requested_limits.append(record_limit)
        return [PaperInput(title=f"Synthetic quota paper {index}", source_id=f"fixture:{index}")
                for index in range(record_limit)], True

    monkeypatch.setattr(app, "online", online)
    monkeypatch.setattr(app.provider, "page", page)
    result = await app.search_papers(project, "session_fixture", SearchQuery(text="fixture"), limit=20)
    assert result["status"] == "succeeded"
    assert len(result["data"]["papers"]) == 2 and result["data"]["effective_candidate_budget"] == 2
    assert result["data"]["stop_reason"] == "permission_budget" and result["data"]["next_cursor"] is None
    repeat = await app.search_papers(project, "session_fixture", SearchQuery(text="another query"))
    assert repeat["error"]["code"] == "BUDGET_EXCEEDED" and requested_limits == [2]


async def test_metadata_rate_requirement_is_applied_to_search(app, monkeypatch):
    app.settings.institutions.append(profile())
    app.provider.last_request["fixture"] = 9
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("cnki_mcp.providers.time.monotonic", lambda: 10)
    monkeypatch.setattr("cnki_mcp.providers.asyncio.sleep", sleep)
    await app.provider.throttle("fixture", "automated_search", "metadata_collect")
    assert sleeps == [4]


def test_parser_does_not_collect_rows_beyond_remaining_budget():
    fixture = '''<table class="result-table-list"><tbody>
        <tr><td class="name"><a class="fz14" href="https://fixture.example/detail?dbcode=CJFD&amp;filename=ONE">Fixture one</a></td></tr>
        <tr><td>Invalid row beyond the allowed collection budget</td></tr>
        </tbody></table>'''
    institution = profile()
    records = extract_results(fixture, "https://fixture.example/", institution, record_limit=1)
    assert len(records) == 1


def test_current_kns_collect_attributes_provide_identity_without_opaque_detail_parameters():
    fixture = '''<table class="result-table-list"><tbody><tr>
        <td class="name"><a class="fz14" href="https://fixture.example/kcms2/article/abstract?v=PRIVATE_DETAIL_TOKEN">Synthetic current layout</a></td>
        <td class="author"><a>Fixture Author</a></td><td class="source">Fixture Journal</td><td class="date">2026-10-19</td>
        <td class="operat"><a class="icon-collect" data-dbname="CJFDLAST2026" data-resource="CJFQ" data-filename="FIXTURE202601001"></a></td>
        </tr></tbody></table>'''
    record, = extract_results(fixture, "https://fixture.example/kns8s/search", profile())
    assert record.source_id == "CJFQ:FIXTURE202601001" and record.paper_type == "journal"
    assert record.detail_url == ("https://fixture.example/kcms2/article/abstract?"
                                 "dbcode=CJFQ&dbname=CJFDLAST2026&filename=FIXTURE202601001")
    assert "PRIVATE_DETAIL_TOKEN" not in record.model_dump_json()
