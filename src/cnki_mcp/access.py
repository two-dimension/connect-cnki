from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from .config import Institution, Settings, validate_url
from .core import AppError, Store, now, require


class BrowserAccess:
    """Only project-owned ephemeral contexts; no saved cookies or personal profiles."""

    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.playwright = None
        self.browser = None
        self.contexts: dict = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.blocked: set[str] = set()
        self.query_pages: dict[str, object] = {}

    async def start(self, institution_id: str, mode: str) -> dict:
        profile = self.settings.institution(institution_id)
        require(mode in profile.access_modes, "ACCESS_MODE_UNAVAILABLE", "学校未核验或未启用此认证模式。")
        session = self.store.put("session", {
            "institution_id": institution_id, "mode": mode, "state": "waiting_for_user",
            "checked_at": None, "institution_name": None,
        })
        self.store.db.commit()
        try:
            from playwright.async_api import async_playwright

            if self.playwright is None:
                self.playwright = await async_playwright().start()
            if self.browser is None or not self.browser.is_connected():
                options = {"headless": self.settings.browser_headless}
                if self.settings.browser_channel != "chromium":
                    options["channel"] = self.settings.browser_channel
                self.browser = await self.playwright.chromium.launch(**options)
            context = await self.browser.new_context(accept_downloads=True)
            context.set_default_timeout(self.settings.network_timeout_ms)
            self.contexts[session["id"]] = context
            self.locks[session["id"]] = asyncio.Lock()

            async def route_request(route):
                try:
                    validate_url(route.request.url, profile.allowed_hosts)
                except (AppError, ValueError):
                    if route.request.is_navigation_request():
                        self.blocked.add(session["id"])
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", route_request)
            page = await context.new_page()
            await page.goto(profile.entry_urls[mode], wait_until="domcontentloaded")
        except Exception:
            if session["id"] in self.contexts:
                await self.contexts.pop(session["id"]).close()
            self.store.put("session", {**session, "state": "paused", "error_code": "BROWSER_SESSION_UNAVAILABLE"})
            self.store.db.commit()
            raise AppError("BROWSER_SESSION_UNAVAILABLE", "专用浏览器启动或入口访问失败。",
                           "检查网络，或运行 uv run playwright install chromium 后重建会话。") from None
        return {**session, "user_action": "请在专用浏览器亲自完成认证，再调用 verify_access_session。"}

    @asynccontextmanager
    async def locked(self, session_id: str):
        require(session_id in self.contexts, "BROWSER_SESSION_UNAVAILABLE", "浏览器上下文已丢失。", "重新建立会话。")
        async with self.locks[session_id]:
            yield self.contexts[session_id]

    def assert_not_blocked(self, session_id: str):
        require(session_id not in self.blocked, "HOST_NOT_ALLOWED", "跳转主机超出学校已核验列表，已暂停。",
                "在本地核验跳转主机；不要向模型提供登录回调或身份数据。")

    async def verify(self, session_id: str) -> dict:
        async with self.locked(session_id) as context:
            return await self.verify_in_context(session_id, context)

    async def verify_in_context(self, session_id: str, context) -> dict:
        session = self.store.get(session_id, "session")
        require(session["state"] != "closed", "BROWSER_SESSION_UNAVAILABLE", "会话已关闭。")
        profile = self.settings.institution(session["institution_id"])
        state, code, detected = "waiting_for_user", "AUTHENTICATION_REQUIRED", None
        if session_id in self.blocked:
            state, code = "paused", "HOST_NOT_ALLOWED"
        else:
            for page in context.pages:
                # Never inspect IdP pages, callbacks, school login DOM, inputs or assertions.
                if urlsplit(page.url).hostname not in profile.provider_hosts:
                    continue
                if any(x in urlsplit(page.url).path.casefold() for x in ("login", "saml", "callback", "auth")):
                    continue
                if await page.locator('input[type="password"]').count():
                    continue
                safety = page.get_by_text("安全验证", exact=True)
                if "安全验证" in await page.title() or (
                    await safety.count() and await safety.first.is_visible()
                ):
                    code = "USER_INTERACTION_REQUIRED"
                    continue
                challenge = page.locator('input[name*="captcha"], input[id*="captcha"], .verify-code')
                if await challenge.count() and await challenge.first.is_visible():
                    code = "USER_INTERACTION_REQUIRED"
                    continue
                if not profile.selectors.institution_label:
                    code = "INSTITUTION_UNVERIFIED"
                    continue
                labels = page.locator(profile.selectors.institution_label)
                if not await labels.count():
                    code = "INSTITUTION_UNVERIFIED"
                    continue
                label = (await labels.first.inner_text()).strip()
                if label in profile.display_names:
                    detected, state, code = label, "ready", None
                    break
                if label:
                    state, code = "paused", "INSTITUTION_MISMATCH"
                    break
        if state == "waiting_for_user" and session["state"] == "ready" and code != "USER_INTERACTION_REQUIRED":
            state, code = "expired", "SESSION_EXPIRED"
        result = {**session, "state": state, "checked_at": now(), "institution_name": detected,
                  "error_code": code, "rules_version": profile.rules_version,
                  "evidence": "exact_institution_label_match" if state == "ready" else "unconfirmed"}
        self.store.put("session", result)
        self.store.db.commit()
        return result

    async def ready(self, session_id: str, project: dict, context) -> Institution:
        session = self.store.get(session_id, "session")
        require(session["institution_id"] == project["institution_id"],
                "INSTITUTION_MISMATCH", "会话学校与项目不一致。")
        require(session["state"] not in {"closed", "paused", "expired"},
                session.get("error_code") or "SESSION_EXPIRED", "会话需由用户重新验证。")
        checked = await self.verify_in_context(session_id, context)
        require(checked["state"] == "ready", checked["error_code"] or "AUTHENTICATION_REQUIRED",
                "目标机构尚未可靠识别。", "完成认证并调用 verify_access_session。")
        return self.settings.institution(session["institution_id"])

    async def close(self, session_id: str, clear_persisted: bool = True) -> dict:
        session = self.store.get(session_id, "session")
        if session_id in self.contexts:
            async with self.locks[session_id]:
                await self.contexts.pop(session_id).close()
        self.blocked.discard(session_id)
        self.store.put("session", {**session, "state": "closed", "error_code": None})
        self.store.db.commit()
        return {"session_id": session_id, "state": "closed", "cleared": "project_ephemeral_context",
                "persisted_state": "never_stored", "school_sso_logout": "not_performed"}

    async def shutdown(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
