from __future__ import annotations

import asyncio
import base64
import html
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlsplit

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
        self.detail_locators: dict[tuple[str, str], str] = {}
        self.redirect_guards: dict = {}
        self.guard_locks: dict = {}
        self.guard_errors: dict[str, set[str]] = {}
        self.blocked_request_shapes: dict[str, list[dict]] = {}
        self.popup_bootstraps: dict[str, set[str]] = {}

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
        except Exception:
            if session["id"] in self.contexts:
                await self.contexts.pop(session["id"]).close()
            self.store.put("session", {**session, "state": "paused", "error_code": "BROWSER_SESSION_UNAVAILABLE"})
            self.store.db.commit()
            raise AppError("BROWSER_SESSION_UNAVAILABLE", "专用浏览器启动失败。",
                           "检查已配置浏览器是否可启动，或选择已安装的浏览器类型。") from None
        action = "请在专用浏览器亲自完成认证，再调用 verify_access_session。"
        try:
            # Opening a session must not wait for all third-party header scripts.
            # The subsequent explicit verification checks the rendered institution.
            response = await page.goto(profile.entry_urls[mode], wait_until="commit")
            if response and response.status >= 400:
                session.update(state="waiting_for_user", error_code="PROVIDER_UNAVAILABLE")
                action = "来源入口返回错误；浏览器已保留，请检查页面后重新核验。"
        except Exception:
            session.update(state="paused" if session["id"] in self.blocked else "waiting_for_user",
                           error_code="HOST_NOT_ALLOWED" if session["id"] in self.blocked else "NETWORK_NOT_READY")
            action = "入口尚未就绪；专用浏览器已保留，请检查网络和页面后重新核验。"
        self.store.put("session", session)
        self.store.db.commit()
        return {**session, "user_action": action}

    async def route_request(self, session_id: str, profile: Institution, route):
        try:
            upgraded = provider_https_upgrade(route.request.url, profile, route.request.method) if route.request.is_navigation_request() else None
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
            try:
                await route.abort()
            except Exception:
                pass
            return
        except Exception:
            self.guard_errors.setdefault(session_id, set()).add("REQUEST_GUARD_UNAVAILABLE")
            self.blocked.add(session_id)
            try:
                await route.abort()
            except Exception:
                pass
            return
        try:
            if session_id in self.contexts:
                try:
                    request_page = route.request.frame.page
                except Exception:
                    # A popup's first navigation has no Playwright Frame yet.
                    # Create a local document first, so its fresh navigation can
                    # install the redirect guard before any remote request leaves.
                    seen = self.popup_bootstraps.setdefault(session_id, set())
                    require(route.request.is_navigation_request() and route.request.method == "GET"
                            and route.request.url not in seen and len(seen) < 20,
                            "HOST_NOT_ALLOWED", "新窗口无法安装跳转检查。")
                    seen.add(route.request.url)
                    destination = html.escape(route.request.url, quote=True)
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
                        body=f'<html><head><meta http-equiv="refresh" content="0;url={destination}"></head>'
                             '<body>正在打开专用窗口…</body></html>')
                    return
                await self.ensure_redirect_guard(session_id, profile.institution_id, request_page)
            await route.fallback()
        except Exception:
            self.guard_errors.setdefault(session_id, set()).add("REQUEST_GUARD_UNAVAILABLE")
            self.blocked.add(session_id)
            try:
                await route.abort()
            except Exception:
                pass

    async def ensure_redirect_guard(self, session_id: str, institution_id: str, page):
        """CDP checks every hop; Playwright routing only intercepts the first redirect hop.

        The first routed request stays paused until this guard is enabled, including
        newly opened pages. Authentication bodies and headers are never accessed.
        """
        async with self.guard_locks.setdefault(page, asyncio.Lock()):
            if page in self.redirect_guards:
                return
            channel = await page.context.new_cdp_session(page)

            async def paused(event):
                request_id = event["requestId"]
                request = event["request"]
                url = request["url"]
                navigation = event.get("resourceType") == "Document"
                try:
                    profile = self.settings.institution(institution_id)
                    if urlsplit(url).scheme in {"about", "data", "blob"}:
                        await channel.send("Fetch.continueRequest", {"requestId": request_id})
                        return
                    upgraded = provider_https_upgrade(url, profile, request["method"]) if navigation else None
                    if upgraded:
                        if not event.get("redirectedRequestId"):
                            # Fresh requests are held by the Playwright route, which
                            # creates the local HTTPS return. Only native redirect hops
                            # need this CDP fulfillment; avoid handling a request twice.
                            await channel.send("Fetch.continueRequest", {"requestId": request_id})
                            return
                        destination = html.escape(upgraded, quote=True)
                        body = (f'<html><head><meta http-equiv="refresh" content="0;url={destination}"></head>'
                                '<body>正在返回知网…</body></html>')
                        await channel.send("Fetch.fulfillRequest", {"requestId": request_id, "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"},
                                                {"name": "Cache-Control", "value": "no-store"},
                                                {"name": "Referrer-Policy", "value": "no-referrer"}],
                            "body": base64.b64encode(body.encode()).decode()})
                        self.https_upgrades[session_id] = self.https_upgrades.get(session_id, 0) + 1
                        return
                    hosts = profile.allowed_hosts
                    if not navigation and event.get("resourceType") in {"Script", "Stylesheet", "Image", "Font", "Media"}:
                        hosts = [*hosts, *profile.resource_hosts]
                    validate_url(url, hosts)
                    await channel.send("Fetch.continueRequest", {"requestId": request_id})
                except (AppError, ValueError):
                    parts = urlsplit(url)
                    hostname = parts.hostname
                    shape = {"scheme": parts.scheme, "host": hostname, "port": parts.port,
                             "method": request["method"], "navigation": navigation,
                             "known_endpoint": parts.path if parts.path in {"/authserver/login", "/idp/authn/RemoteUser"} else "other"}
                    shapes = self.blocked_request_shapes.setdefault(session_id, [])
                    if shape not in shapes and len(shapes) < 24:
                        shapes.append(shape)
                    category = "navigation" if navigation else "resources"
                    blocked = self.blocked_hosts.setdefault(session_id, {"navigation": set(), "resources": set()})
                    if hostname:
                        blocked[category].add(hostname)
                    if navigation:
                        self.blocked.add(session_id)
                    try:
                        await channel.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    except Exception:
                        pass
                except Exception:
                    self.guard_errors.setdefault(session_id, set()).add("REDIRECT_GUARD_UNAVAILABLE")
                    self.blocked.add(session_id)
                    try:
                        await channel.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    except Exception:
                        pass

            channel.on("Fetch.requestPaused", paused)
            await channel.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            self.redirect_guards[page] = channel

            def closed():
                self.redirect_guards.pop(page, None)
                self.guard_locks.pop(page, None)

            page.on("close", closed)

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
                if await page.locator('input[type="password"]:visible').count():
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
                item["path"] = url.path[:180]
                configured_search = urlsplit(profile.search_url)
                search_base = configured_search.path.rstrip("/")
                if url.hostname == configured_search.hostname and (
                    url.path.rstrip("/") == search_base or url.path.startswith(search_base + "/")
                ):
                    # Public search controls only: never field values, login DOM or page HTML.
                    widgets = page.locator('input[type="text"]:visible, input:not([type]):visible, '
                                           'button:visible, select:visible, ' + profile.selectors.search_button)
                    item["search_widgets"] = []
                    for index in range(min(await widgets.count(), 24)):
                        widget = widgets.nth(index)
                        if not await widget.is_visible():
                            continue
                        structure = await widget.evaluate("""element => ({
                            tag: element.tagName.toLowerCase(), id: element.id,
                            classes: typeof element.className === 'string' ? element.className : '',
                            ancestors: [element.parentElement,element.parentElement?.parentElement,
                                element.parentElement?.parentElement?.parentElement].filter(node => node && node.tagName !== 'BODY')
                                .map(node => ({tag:node.tagName.toLowerCase(),id:node.id,classes:typeof node.className === 'string' ? node.className : ''})),
                            label: element.tagName === 'INPUT' ? (element.getAttribute('placeholder') || '')
                                   : (element.innerText || '').slice(0,160)
                        })""")
                        if re.search(r"saml|cookie|token|password|session|secret|captcha", structure["id"], re.I):
                            continue
                        item["search_widgets"].append(structure)
                    item["known_search_choices"] = []
                    for label in ("主题", "篇名", "篇关摘", "关键词", "作者", "来源", "文献来源", "相关度", "发表时间", "被引", "下载", "高级检索"):
                        choices = page.get_by_text(label, exact=True)
                        for index in range(min(await choices.count(), 4)):
                            choice = choices.nth(index)
                            choice_shape = await choice.evaluate("""element => ({
                                tag: element.tagName.toLowerCase(), id: element.id,
                                classes: typeof element.className === 'string' ? element.className : '',
                                parent_id: element.parentElement?.id || '',
                                parent_classes: typeof element.parentElement?.className === 'string' ? element.parentElement.className : '',
                                ancestors: [element.parentElement,element.parentElement?.parentElement,
                                    element.parentElement?.parentElement?.parentElement].filter(node => node && node.tagName !== 'BODY')
                                    .map(node => ({tag:node.tagName.toLowerCase(),id:node.id,classes:typeof node.className === 'string' ? node.className : ''}))
                            })""")
                            item["known_search_choices"].append({"label": label, "visible": await choice.is_visible(), **choice_shape})
                    item["result_layout"] = []
                    rows = page.locator(profile.selectors.result_rows)
                    for index in range(min(await rows.count(), 3)):
                        row = rows.nth(index)
                        shape = await row.evaluate("""element => ({
                            attribute_names: Array.from(element.attributes, attr => attr.name),
                            cells: Array.from(element.querySelectorAll('td'), cell => ({classes: cell.className})),
                            links: Array.from(element.querySelectorAll('a'), link => ({
                                id: link.id, classes: link.className, href: link.href,
                                attribute_names: Array.from(link.attributes, attr => attr.name),
                                source_fields: Object.fromEntries(['data-dbname','data-filename','data-resource']
                                    .filter(name => link.hasAttribute(name)).map(name => [name, link.getAttribute(name)]))
                            })).slice(0,8),
                            checkboxes: Array.from(element.querySelectorAll('input[type=checkbox]'), input => ({
                                name: input.name, id: input.id, classes: input.className
                            }))
                        })""")
                        for link in shape["links"]:
                            link["source_fields"] = {key: value for key, value in link["source_fields"].items()
                                                     if re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", value)}
                            target = urlsplit(link.pop("href"))
                            link["target_shape"] = {"host": target.hostname, "path": target.path,
                                                    "query_keys": [key for key, _ in parse_qsl(target.query)]}
                        item["result_layout"].append(shape)
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
        has_interaction = any(page["kind"] in {"authentication", "identity_provider", "human_verification"} for page in pages)
        return {"session_id": session_id, "identity_verified": session["state"] == "ready"
                and session_id not in self.blocked and not has_interaction,
                "open_page_count": len(pages),
                "configured_institution_selector": bool(profile.selectors.institution_label),
                "https_return_upgrades": self.https_upgrades.get(session_id, 0),
                "guard_errors": sorted(self.guard_errors.get(session_id, set())),
                "blocked_requests": self.blocked_request_shapes.get(session_id, []),
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
            pending_interaction = False
            for page in context.pages:
                # Never inspect IdP pages, callbacks, school login DOM, inputs or assertions.
                if urlsplit(page.url).hostname not in profile.provider_hosts:
                    if urlsplit(page.url).hostname in profile.allowed_hosts:
                        pending_interaction = True
                        code = "AUTHENTICATION_REQUIRED"
                    continue
                if any(x in urlsplit(page.url).path.casefold() for x in ("login", "saml", "callback", "auth")):
                    pending_interaction = True
                    continue
                if await page.locator('input[type="password"]:visible').count():
                    pending_interaction = True
                    continue
                safety = page.get_by_text("安全验证", exact=True)
                if "安全验证" in await page.title() or (
                    await safety.count() and await safety.first.is_visible()
                ):
                    code = "USER_INTERACTION_REQUIRED"
                    pending_interaction = True
                    break
                challenge = page.locator('input[name*="captcha"], input[id*="captcha"], .verify-code')
                if await challenge.count() and await challenge.first.is_visible():
                    code = "USER_INTERACTION_REQUIRED"
                    pending_interaction = True
                    break
                if not profile.selectors.institution_label:
                    code = "INSTITUTION_UNVERIFIED"
                    continue
                labels = page.locator(profile.selectors.institution_label)
                visible = None
                for index in range(await labels.count()):
                    if await labels.nth(index).is_visible():
                        visible = labels.nth(index)
                        break
                if visible is None:
                    code = "INSTITUTION_UNVERIFIED"
                    continue
                label = (await visible.inner_text()).strip()
                if label in profile.display_names:
                    detected = label
                    continue
                if label:
                    state, code = "paused", "INSTITUTION_MISMATCH"
                    break
            if detected and not pending_interaction and state != "paused":
                state, code = "ready", None
            else:
                detected = None
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
        self.guard_errors.pop(session_id, None)
        self.blocked_request_shapes.pop(session_id, None)
        self.popup_bootstraps.pop(session_id, None)
        for key in [key for key in self.detail_locators if key[0] == session_id]:
            self.detail_locators.pop(key, None)
        self.store.put("session", {**session, "state": "closed", "error_code": None})
        self.store.db.commit()
        return {"session_id": session_id, "state": "closed", "cleared": "project_ephemeral_context",
                "persisted_state": "never_stored", "school_sso_logout": "not_performed"}

    async def shutdown(self):
        self.detail_locators.clear()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.redirect_guards.clear()
        self.guard_locks.clear()
        self.popup_bootstraps.clear()
