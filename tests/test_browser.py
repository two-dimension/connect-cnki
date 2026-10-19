import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from cnki_mcp.config import Institution, PolicyRecord, Selectors
from cnki_mcp.core import AppError
from cnki_mcp.models import SearchQuery


@pytest.mark.browser
async def test_real_browser_synthetic_search_pagination_and_identity(app):
    fixture = (Path(__file__).parent / "fixtures" / "cnki_synthetic.html").read_text(encoding="utf-8")
    fixture = fixture.replace('<input id="txt_SearchText">', '''<div class="search-main">
        <div class="sort-default" onclick="document.querySelector('#fixture-fields').hidden=false"><span>主题</span></div>
        <ul id="fixture-fields" hidden><li><a onclick="document.querySelector('.sort-default span').textContent='篇名'; this.parentElement.parentElement.hidden=true">篇名</a></li></ul>
        <input id="txt_SearchText">''').replace('</button>\n<table', '</button></div>\n<table', 1)
    fixture = fixture.replace("</html>", '''<ul><li id="fixture-relevance"
        onclick="this.className='cur'; setTimeout(()=>render(1),70)">相关度</li></ul></html>''')
    app.settings.browser_headless = True
    profile = Institution(
        institution_id="fixture", display_names=["厦门大学"], access_modes=["institution_network", "carsi"],
        entry_urls={"institution_network": "https://fixture.example/", "carsi": "https://fixture.example/"},
        search_url="https://fixture.example/", allowed_hosts=["fixture.example"], provider_hosts=["fixture.example"],
        selectors=Selectors(institution_label=".institution", default_sort="latest", sort_active_class="cur",
                            sort_controls={"relevance": "#fixture-relevance"},
                            search_field_trigger=".search-main .sort-default", search_field_label=".search-main .sort-default > span",
                            search_fields={"title": '.search-main a:text-is("篇名")'},
                            search_field_labels={"subject": "主题", "title": "篇名"}),
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
        await page.locator("#txt_SearchText").fill("PRIVATE_SEARCH_FIELD_VALUE")
        inspected = await app.get_access_status(sid, inspect_page=True)
        widgets = inspected["data"]["browser_diagnostics"]["pages"][0]["search_widgets"]
        assert any(widget["id"] == "txt_SearchText" for widget in widgets)
        assert "PRIVATE_SEARCH_FIELD_VALUE" not in str(inspected)
        pending = await context.new_page()
        await pending.goto("https://fixture.example/")
        await pending.evaluate("document.title = '安全验证'")
        blocked = await app.verify_access_session(sid)
        assert blocked["data"]["state"] == "waiting_for_user"
        assert blocked["data"]["error_code"] == "USER_INTERACTION_REQUIRED"
        await pending.close()
        assert (await app.verify_access_session(sid))["data"]["state"] == "ready"
        project = (await app.create_project("fixture", "fixture"))["data"]["project_id"]
        query = SearchQuery(text="prediction")
        first = await app.search_papers(project, sid, query, limit=1)
        assert first["status"] == "succeeded", first
        layout = await app.get_access_status(sid, inspect_page=True)
        assert any(item.get("result_layout") for item in layout["data"]["browser_diagnostics"]["pages"])
        assert "PRIVATE_TOKEN" not in str(layout)
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
        titled = await app.search_papers(project, sid, SearchQuery(text="fixture title query", field="title"), limit=1)
        assert titled["status"] == "succeeded", titled
        query_page = app.access.query_pages[titled["data"]["query_id"]]
        assert await query_page.locator(".search-main .sort-default > span").inner_text() == "篇名"
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
        diagnostic = await app.access.inspect(sid)
        assert diagnostic["pages"][0]["kind"] == "human_verification"
        assert not diagnostic["identity_verified"]
        await page.evaluate("document.title = 'Fixture provider'")
        await page.locator(".institution").evaluate("element => element.textContent = '厦门大学'")
        profile.selectors.institution_label = ""
        diagnostic = await app.access.inspect(sid)
        assert diagnostic["pages"][0]["institution_candidates"][0]["selector_candidates"] == ["div.institution"]
        assert not diagnostic["identity_verified"]  # A diagnostic never marks an arbitrary matching body label ready.
        # The real header embeds an unopened login panel. Hidden fields do not make
        # the whole provider page an authentication form; values remain unread.
        profile.selectors.institution_label = ".institution"
        await page.set_content('<input type="password" hidden value="PRIVATE_HIDDEN_PASSWORD"><div class="institution">厦门大学</div>')
        diagnostic = await app.access.inspect(sid)
        assert diagnostic["pages"][0]["kind"] == "provider"
        assert "PRIVATE_HIDDEN_PASSWORD" not in str(diagnostic)
        assert (await app.verify_access_session(sid))["data"]["state"] == "ready"
        await page.locator(".institution").evaluate("element => element.hidden = true")
        assert (await app.verify_access_session(sid))["data"]["state"] != "ready"
        await page.locator(".institution").evaluate("element => element.hidden = false")
        # Login DOM, query assertions and password values never enter the diagnostic.
        await page.set_content('<input type="password" value="PRIVATE_PASSWORD"><div class="institution">厦门大学</div>')
        diagnostic = await app.access.inspect(sid)
        assert diagnostic["pages"][0]["kind"] == "authentication"
        assert not diagnostic["pages"][0]["institution_candidates"]
        assert "PRIVATE_PASSWORD" not in str(diagnostic)
        idp = await context.new_page()
        await idp.goto("https://fixture.example/SAML/callback?SAMLResponse=PRIVATE_ASSERTION")
        diagnostic = await app.access.inspect(sid)
        assert diagnostic["pages"][1]["kind"] == "authentication"
        assert "PRIVATE_ASSERTION" not in str(diagnostic)
        await page.close()
        await idp.close()
        missing = await app.verify_access_session(sid)
        assert missing["data"]["state"] == "expired"
        assert missing["data"]["error_code"] == "BROWSER_SESSION_UNAVAILABLE"
        await browser.close()
        app.access.contexts.clear()


@pytest.mark.browser
async def test_browser_upgrades_solved_challenge_return_without_http_egress(app):
    profile = Institution(institution_id="fixture", display_names=["Fixture"],
        allowed_hosts=["fixture.example"], provider_hosts=["fixture.example"],
        access_modes=["institution_network"], entry_urls={"institution_network": "https://fixture.example/kns8s/"},
        search_url="https://fixture.example/kns8s/")
    received = []
    async with async_playwright() as playwright:
        options = {"headless": True}
        if os.environ.get("CNKI_TEST_BROWSER_CHANNEL"):
            options["channel"] = os.environ["CNKI_TEST_BROWSER_CHANNEL"]
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        app.settings.institutions.append(profile)
        with app.store.transaction():
            app.store.put("session", {"id": "fixture_session", "institution_id": "fixture",
                                      "mode": "institution_network", "state": "waiting_for_user"})
        app.access.contexts["fixture_session"] = context
        app.access.locks["fixture_session"] = asyncio.Lock()
        await context.add_cookies([{"name": "fixture_session_cookie", "value": "TEST_ONLY", "url": "https://fixture.example"}])

        async def server_fixture(route):
            received.append(route.request.url)
            if route.request.url.endswith("/external-redirect"):
                await route.fulfill(status=302, headers={
                    "location": "https://unapproved.fixture.example/login?SAMLRequest=PRIVATE_REDIRECT_ASSERTION"})
                return
            if route.request.url.endswith("/http-return-redirect"):
                await route.fulfill(status=302, headers={
                    "location": "http://fixture.example/kns8s/?captchaId=PRIVATE_NATIVE_RETURN"})
                return
            await route.fulfill(status=200, content_type="text/html", body="<p>Synthetic return completed</p>")

        await context.route("**/*", server_fixture)
        await context.route("**/*", lambda route: app.access.route_request(
            "fixture_session", app.access.settings.institution("fixture"), route))
        page = await context.new_page()
        await page.goto("http://fixture.example/kns8s/?captchaId=PRIVATE_TEST_NONCE&encoded=a%2Fb%3D", wait_until="domcontentloaded")
        await page.wait_for_url("https://fixture.example/**", wait_until="domcontentloaded")
        assert received == ["https://fixture.example/kns8s/?captchaId=PRIVATE_TEST_NONCE&encoded=a%2Fb%3D"]
        assert app.access.https_upgrades["fixture_session"] == 1
        assert "fixture_session" not in app.access.blocked
        updated = app.settings.model_copy(deep=True)
        updated.institutions[0].resource_hosts = ["static.fixture.example"]
        reloaded = await app.access.reconfigure("fixture_session", updated)
        assert reloaded["browser_context_preserved"]
        assert app.access.contexts["fixture_session"] is context
        assert any(cookie["value"] == "TEST_ONLY" for cookie in await context.cookies())
        async with page.expect_response("https://static.fixture.example/image.png"):
            await page.locator("body").evaluate(
                "body => {const image = new Image(); image.src = 'https://static.fixture.example/image.png'; body.append(image)}")
        assert received[-1] == "https://static.fixture.example/image.png"
        await page.goto("https://fixture.example/kns8s/http-return-redirect", wait_until="domcontentloaded")
        await page.wait_for_url("https://fixture.example/kns8s/?captchaId=PRIVATE_NATIVE_RETURN", wait_until="domcontentloaded")
        assert received[-1] == "https://fixture.example/kns8s/?captchaId=PRIVATE_NATIVE_RETURN"
        assert not any(url.startswith("http:") for url in received)
        assert not app.access.guard_errors.get("fixture_session")
        with pytest.raises(AppError) as failure:
            await app.provider.goto(page, "https://fixture.example/kns8s/external-redirect", updated.institutions[0], "fixture_session")
        assert failure.value.code == "HOST_NOT_ALLOWED"
        assert sum(url.endswith("/external-redirect") for url in received) == 1
        assert "unapproved.fixture.example" in app.access.blocked_hosts["fixture_session"]["navigation"]
        assert "fixture_session" in app.access.blocked
        assert "PRIVATE_REDIRECT_ASSERTION" not in str(await app.access.inspect("fixture_session"))
        await browser.close()
        app.access.contexts.clear()
