from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from cnki_mcp.config import Institution, PolicyRecord
from cnki_mcp.models import PaperInput, SearchFilters, SearchQuery
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
                for index in range(record_limit)], True, True

    monkeypatch.setattr(app, "online", online)
    monkeypatch.setattr(app.provider, "page", page)
    result = await app.search_papers(project, "session_fixture", SearchQuery(text="fixture"), limit=20)
    assert result["status"] == "succeeded"
    assert len(result["data"]["papers"]) == 2 and result["data"]["effective_candidate_budget"] == 2
    assert result["data"]["stop_reason"] == "permission_budget" and result["data"]["next_cursor"] is None
    repeat = await app.search_papers(project, "session_fixture", SearchQuery(text="another query"))
    assert repeat["error"]["code"] == "BUDGET_EXCEEDED" and requested_limits == [2]


async def test_truncated_page_with_existing_duplicates_stops_without_skipping_tail(app, monkeypatch):
    institution = profile()
    app.settings.institutions.append(institution)
    project = (await app.create_project("Existing duplicate fixture", "fixture", budget=2))["data"]["project_id"]
    record = PaperInput(title="Existing fixture paper", authors=["Fixture Author"], year=2024,
                        source_id="fixture:existing", source_provider="cnki")
    rights = {"origin": "institution", "institution_id": "fixture", "resource_type": "journal"}
    app.library.ingest(project, record, rights, "cnki_search")
    app.store.db.commit()

    @asynccontextmanager
    async def online(*args):
        yield object(), institution

    async def page(*args, record_limit=None):
        assert record_limit == 1
        return [record], True, True  # An unseen second row remains on this source page.

    monkeypatch.setattr(app, "online", online)
    monkeypatch.setattr(app.provider, "page", page)
    result = await app.search_papers(project, "session_fixture", SearchQuery(text="fixture"))
    assert result["status"] == "succeeded", result
    assert len(app.store.papers(project)) == 1
    assert result["data"]["stop_reason"] == "source_page_budget"
    assert result["data"]["source_page_truncated"] is True
    assert result["data"]["next_cursor"] is None and result["data"]["exhaustive"] is False


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


async def test_explicit_local_filters_preserve_all_candidates_and_exclusion_reasons(app, monkeypatch):
    institution = profile()
    app.settings.institutions.append(institution)
    project = (await app.create_project("Local filter fixture", "fixture", budget=10))["data"]["project_id"]
    records = [PaperInput(title=f"Filter fixture {index}", authors=["Fixture Author"], year=year, paper_type=kind)
               for index, (year, kind) in enumerate([(2024, "journal"), (2020, "journal"), (2024, "thesis"), (None, "journal")])]

    @asynccontextmanager
    async def online(*args):
        yield object(), institution

    async def page(*args, record_limit=None):
        assert args[5] == SearchFilters()  # The source is not claimed to apply these conditions.
        return records, False, False

    monkeypatch.setattr(app, "online", online)
    monkeypatch.setattr(app.provider, "page", page)
    filters = SearchFilters(year_from=2023, year_to=2025, paper_types=["journal"])
    result = await app.search_papers(project, "session_fixture", SearchQuery(text="fixture"), filters=filters,
                                     filter_mode="local_candidates", limit=4)
    assert result["status"] == "succeeded", result
    assert len(result["data"]["papers"]) == 1 and result["data"]["papers"][0]["year"] == 2024
    assert result["data"]["collected"] == 4 and result["data"]["matched_count"] == 1
    assert result["data"]["filtered_out_count"] == 3 and result["warnings"]
    assert len(app.store.papers(project)) == 4 and len(app.store.all("query_filter")) == 3
    run = app.store.get(result["data"]["query_id"], "query")
    assert {reason for decision in run["filter_exclusions"] for reason in decision["reasons"]} == {
        "year_unknown", "year_outside_range", "paper_type_outside_scope",
    }


async def test_local_filter_mode_cannot_replace_a_type_limited_collection_permission(app, monkeypatch):
    institution = profile()
    for policy in institution.policy_records:
        policy.resource_types = ["journal"]
    app.settings.institutions.append(institution)
    project = (await app.create_project("Restricted collection fixture", "fixture"))["data"]["project_id"]
    visited = False

    @asynccontextmanager
    async def online(*args):
        nonlocal visited
        visited = True
        yield object(), institution

    monkeypatch.setattr(app, "online", online)
    result = await app.search_papers(project, "session_fixture", SearchQuery(text="fixture"),
        filters=SearchFilters(paper_types=["journal"]), filter_mode="local_candidates")
    assert result["error"]["code"] == "POLICY_SCOPE_REQUIRED" and not visited


async def test_unknown_default_sort_is_rejected_before_source_navigation(app):
    from cnki_mcp.core import AppError

    with pytest.raises(AppError) as failure:
        await app.provider.page(object(), profile(), "fixture_session", "fixture_query", SearchQuery(text="fixture"),
                                SearchFilters(), "relevance", 1)
    assert failure.value.code == "UNSUPPORTED_SORT"


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
        <td class="name"><a class="fz14" href="https://fixture.example/kcms2/article/abstract?v=PRIVATE_DETAIL_TOKEN">Synthetic <em>current</em> layout</a></td>
        <td class="author"><a>Fixture Author</a><a class="showAllAuthors"></a></td><td class="source">Fixture Journal</td><td class="date">2026-10-19</td>
        <td class="operat"><a class="icon-collect" data-dbname="CJFDLAST2026" data-resource="CJFQ" data-filename="FIXTURE202601001"></a></td>
        </tr></tbody></table>'''
    locators = {}
    record, = extract_results(fixture, "https://fixture.example/kns8s/search", profile(), detail_locators=locators)
    assert record.source_id == "CJFQ:FIXTURE202601001" and record.paper_type == "journal"
    assert record.title == "Synthetic current layout"
    assert record.authors == ["Fixture Author"]
    assert record.detail_url is None
    assert locators[record.source_id] == "https://fixture.example/kcms2/article/abstract?v=PRIVATE_DETAIL_TOKEN"
    assert "PRIVATE_DETAIL_TOKEN" not in record.model_dump_json()


async def test_detail_locators_are_session_scoped_and_cleared_on_close(app):
    session = app.store.put("session", {"institution_id": "fixture", "state": "ready"})
    paper = {"source_provider": "cnki", "source_id": "CJFQ:FIXTURE", "detail_url": None}
    locator = "https://fixture.example/kcms2/article/abstract?v=PRIVATE_DETAIL_TOKEN"
    app.access.detail_locators[(session["id"], paper["source_id"])] = locator
    assert app.provider.detail_locator(session["id"], paper) == locator
    from cnki_mcp.core import AppError

    with pytest.raises(AppError) as failure:
        app.provider.detail_locator("another_session", paper)
    assert failure.value.code == "DETAIL_LINK_EXPIRED"
    closed = await app.close_access_session(session["id"])
    assert "PRIVATE_DETAIL_TOKEN" not in str(closed)
    assert not app.access.detail_locators
