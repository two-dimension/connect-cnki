from __future__ import annotations

import asyncio
import html
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from .config import Institution, Settings, provider_https_upgrade, validate_url
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
        self.blocked_hosts: dict[str, dict[str, set[str]]] = {}
        self.https_upgrades: dict[str, int] = {}
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
            context = await self.browser.new_context(accept_downloads=True, service_workers="block")
            context.set_default_timeout(self.settings.network_timeout_ms)
            self.contexts[session["id"]] = context
            self.locks[session["id"]] = asyncio.Lock()
            self.blocked_hosts[session["id"]] = {"navigation": set(), "resources": set()}

            async def route_request(route):
                await self.route_request(session["id"], self.settings.institution(institution_id), route)

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

    async def route_request(self, session_id: str, profile: Institution, route):
        try:
            upgraded = provider_https_upgrade(route.request.url, profile, route.request.method)
            if upgraded:
                # A local navigation document forces a fresh intercepted HTTPS request.
                # Playwright's native 3xx continuation does not re-run route handlers.
                self.https_upgrades[session_id] = self.https_upgrades.get(session_id, 0) + 1
                destination = html.escape(upgraded, quote=True)
                await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                    headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
                    body=f'<html><head><meta http-equiv="refresh" content="0;url={destination}"></head>'
                         '<body>正在返回知网…</body></html>')
                return
            hosts = profile.allowed_hosts
            if not route.request.is_navigation_request() and route.request.resource_type in {
                "script", "stylesheet", "image", "font", "media",
            }:
                hosts = [*hosts, *profile.resource_hosts]
            validate_url(route.request.url, hosts)
        except (AppError, ValueError):
            hostname = urlsplit(route.request.url).hostname
            category = "navigation" if route.request.is_navigation_request() else "resources"
            hosts = self.blocked_hosts.setdefault(session_id, {"navigation": set(), "resources": set()})
            if hostname:
                hosts[category].add(hostname)
            if route.request.is_navigation_request():
                self.blocked.add(session_id)
            await route.abort()
            return
        await route.fallback()

    async def reconfigure(self, session_id: str, settings: Settings) -> dict:
        """Load a maintainer's validated local profile without replacing browser cookies."""
        session = self.store.get(session_id, "session")
        profile = settings.institution(session["institution_id"])
        require(session["mode"] in profile.access_modes, "ACCESS_MODE_UNAVAILABLE", "新配置未启用当前认证模式。")
        require(settings.data_dir.resolve() == self.settings.data_dir.resolve()
                and settings.browser_channel == self.settings.browser_channel,
                "CONFIG_RESTART_REQUIRED", "数据目录或浏览器类型变化需要重启诊断会话。")
        async with self.locked(session_id):
            self.settings = settings
            self.store.put("session", {**session, "state": "waiting_for_user", "checked_at": None,
                                        "institution_name": None, "error_code": None})
            self.store.db.commit()
        return {"session_id": session_id, "reloaded": True, "browser_context_preserved": True,
                "institution_id": profile.institution_id, "rules_version": profile.rules_version}

    async def reopen(self, session_id: str) -> dict:
        """Explicitly reopen only the configured entry, retaining this dedicated context."""
        session = self.store.get(session_id, "session")
        profile = self.settings.institution(session["institution_id"])
        entry = validate_url(profile.entry_urls[session["mode"]], profile.allowed_hosts)
        async with self.locked(session_id) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            self.blocked.discard(session_id)
            self.blocked_hosts[session_id] = {"navigation": set(), "resources": set()}
            self.store.put("session", {**session, "state": "waiting_for_user", "checked_at": None,
                                        "institution_name": None, "error_code": None})
            self.store.db.commit()
            try:
                await page.goto(entry, wait_until="domcontentloaded")
            except Exception:
                raise AppError("NETWORK_NOT_READY", "专用入口重新打开失败。", "检查网络或 inspect 中的脱敏主机状态。") from None
        return {"session_id": session_id, "entry_reopened": True, "browser_context_preserved": True}

    async def inspect(self, session_id: str) -> dict:
        """Read only known provider labels and structural hints, never login DOM or URLs.

        This local diagnostic does not establish identity or change a configured rule.
        It helps maintainers pin a genuine institution widget after human verification.
        """
        session = self.store.get(session_id, "session")
        profile = self.settings.institution(session["institution_id"])
        pages = []
        async with self.locked(session_id) as context:
            for page in context.pages:
                url = urlsplit(page.url)
                item = {"host": url.hostname, "kind": "unverified", "institution_candidates": []}
                if url.hostname not in profile.provider_hosts:
                    item["kind"] = "identity_provider" if url.hostname in profile.allowed_hosts else "unverified"
                    pages.append(item)
                    continue
                if url.scheme != "https" or any(
                    word in url.path.casefold() for word in ("login", "saml", "callback", "auth", "shibboleth")
                ):
                    item["kind"] = "authentication"
                    pages.append(item)
                    continue
                if await page.locator('input[type="password"]').count():
                    item["kind"] = "authentication"
                    pages.append(item)
                    continue
                safety = page.get_by_text("安全验证", exact=True)
                if "安全验证" in await page.title() or (
                    await safety.count() and await safety.first.is_visible()
                ):
                    item["kind"] = "human_verification"
                    pages.append(item)
                    continue
                item["kind"] = "provider"
                for name in profile.display_names:
                    matches = page.get_by_text(name, exact=True)
                    for index in range(min(await matches.count(), 5)):
                        element = matches.nth(index)
                        if not await element.is_visible():
                            continue
                        structure = await element.evaluate(
                            "element => ({tag: element.tagName.toLowerCase(), id: element.id, classes: element.className})"
                        )
                        tag = structure["tag"]
                        tokens = [structure["id"], *str(structure["classes"]).split()]
                        safe_tokens = [token for token in tokens if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,47}", token)
                                       and not re.search(r"saml|cookie|token|password|session|secret", token, re.I)]
                        suggestions = []
                        if structure["id"] in safe_tokens:
                            suggestions.append(f"#{structure['id']}")
                        suggestions.extend(f"{tag}.{token}" for token in safe_tokens if token != structure["id"])
                        item["institution_candidates"].append({"expected_name": name, "tag": tag,
                                                               "selector_candidates": suggestions})
                pages.append(item)
        blocked = self.blocked_hosts.get(session_id, {"navigation": set(), "resources": set()})
        return {"session_id": session_id, "identity_verified": session["state"] == "ready",
                "open_page_count": len(pages),
                "configured_institution_selector": bool(profile.selectors.institution_label),
                "https_return_upgrades": self.https_upgrades.get(session_id, 0),
                "pages": pages, "blocked_hosts": {key: sorted(value) for key, value in blocked.items()},
                "note": "候选节点仅供维护者核验；学校名出现在正文不能证明机构认证。"}

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
        if not context.pages:
            state, code = "expired", "BROWSER_SESSION_UNAVAILABLE"
        elif session_id in self.blocked:
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
        self.blocked_hosts.pop(session_id, None)
        self.https_upgrades.pop(session_id, None)
        self.store.put("session", {**session, "state": "closed", "error_code": None})
        self.store.db.commit()
        return {"session_id": session_id, "state": "closed", "cleared": "project_ephemeral_context",
                "persisted_state": "never_stored", "school_sso_logout": "not_performed"}

    async def shutdown(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
