import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from cnki_mcp.config import Institution, PolicyRecord, Selectors
from cnki_mcp.models import SearchQuery


@pytest.mark.browser
async def test_real_browser_synthetic_search_pagination_and_identity(app):
    fixture = (Path(__file__).parent / "fixtures" / "cnki_synthetic.html").read_text(encoding="utf-8")
    app.settings.browser_headless = True
    profile = Institution(
        institution_id="fixture", display_names=["厦门大学"], access_modes=["institution_network", "carsi"],
        entry_urls={"institution_network": "https://fixture.example/", "carsi": "https://fixture.example/"},
        search_url="https://fixture.example/", allowed_hosts=["fixture.example"], provider_hosts=["fixture.example"],
        selectors=Selectors(institution_label=".institution"),
        policy_records=[PolicyRecord(operation=operation, state="allowed", source="Synthetic test permission",
            verified_by="test_suite", verified_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=1), resource_types=["*"], min_interval_seconds=0.1)
            for operation in ("automated_search", "metadata_collect")],
    )
    app.settings.institutions.append(profile)
    async with async_playwright() as playwright:
        options = {"headless": True}
        if os.environ.get("CNKI_TEST_BROWSER_CHANNEL"):
            options["channel"] = os.environ["CNKI_TEST_BROWSER_CHANNEL"]
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()

        async def fulfill(route):
            await route.fulfill(status=200, content_type="text/html", body=fixture)

        await context.route("**/*", fulfill)
        page = await context.new_page()
        await page.goto("https://fixture.example/")
        with app.store.transaction():
            session = app.store.put("session", {"institution_id": "fixture", "mode": "institution_network", "state": "waiting_for_user"})
        sid = session["id"]
        app.access.contexts[sid] = context
        app.access.locks[sid] = asyncio.Lock()
        assert (await app.verify_access_session(sid))["data"]["state"] == "ready"
        project = (await app.create_project("fixture", "fixture"))["data"]["project_id"]
        query = SearchQuery(text="prediction")
        first = await app.search_papers(project, sid, query, limit=1)
        assert first["status"] == "succeeded", first
        cursor = first["next_cursor"]
        assert "PRIVATE_TOKEN" not in str(first)
        second = await app.search_papers(project, sid, query, cursor=cursor, limit=1)
        assert second["data"]["pages_read"] == 1  # Remaining cached row, no second network page.
        replay = await app.search_papers(project, sid, query, cursor=cursor, limit=1)
        assert replay["error"]["code"] == "CURSOR_CONSUMED"
        third = await app.search_papers(project, sid, query, cursor=second["next_cursor"], limit=2)
        assert third["data"]["pages_read"] == 2, third
        assert third["data"]["stop_reason"] == "source_end"
        assert {p["title"] for p in third["data"]["papers"]} == {"Fixture paper 21", "Fixture paper 22"}
        # A public homepage and any school name in arbitrary body text must not pass identity checks.
        await page.locator(".institution").evaluate("element => element.textContent = '其他大学'")
        for extra in context.pages[1:]:
            await extra.close()
        mismatch = await app.verify_access_session(sid)
        assert mismatch["data"]["error_code"] == "INSTITUTION_MISMATCH"
        assert mismatch["data"]["state"] == "paused"
        await page.evaluate("document.title = '安全验证'")
        challenge = await app.verify_access_session(sid)
        assert challenge["data"]["error_code"] == "USER_INTERACTION_REQUIRED"
        assert challenge["data"]["state"] == "waiting_for_user"
        await browser.close()
        app.access.contexts.clear()
