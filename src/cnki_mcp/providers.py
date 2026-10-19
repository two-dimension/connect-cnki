from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .access import BrowserAccess
from .config import Institution, Settings, public_url, re_safe, validate_url
from .core import AppError, require
from .models import PaperInput, SearchFilters, SearchQuery
from .policy import PolicyGate


def extract_results(html: str, base_url: str, profile: Institution, record_limit: int | None = None,
                    detail_locators: dict[str, str] | None = None) -> list[PaperInput]:
    soup, records = BeautifulSoup(html, "html.parser"), []
    selectors = profile.selectors
    rows = soup.select(selectors.result_rows)
    if not rows:
        require(bool(soup.select(selectors.empty_results)), "SOURCE_LAYOUT_CHANGED", "检索结果结构未知，停止采集。")
    for row in rows[:record_limit]:
        link = row.select_one(selectors.title_link)
        require(link and link.get("href") and link.get_text(strip=True),
                "SOURCE_LAYOUT_CHANGED", "结果行缺少题名或详情地址。")
        url = urljoin(base_url, link["href"])
        validate_url(url, profile.provider_hosts)
        args = {k.lower(): v for k, v in parse_qs(urlsplit(url).query).items()}
        filename, dbcode, dbname = (args.get(key, [""])[0] for key in ("filename", "dbcode", "dbname"))
        session_bound_link = not filename and "v" in args
        if not filename:
            # Current KNS links contain an opaque v parameter. Stable bibliographic
            # identifiers are exposed on the row's collect control instead.
            marker = row.select_one("[data-dbname][data-filename]")
            if marker:
                filename, dbname = marker.get("data-filename", ""), marker.get("data-dbname", "")
                resource = str(marker.get("data-resource", "")).upper()
                for candidate in (resource, dbname.upper()):
                    match = re.match(r"^(CJFQ|CJFD|CAPJ|CDFD|CMFD|CPFD|CCND)", candidate)
                    if match:
                        dbcode = match[1]
                        break
        require(filename and (dbcode or dbname) and all(re_safe(value) for value in (filename, dbcode or dbname)),
                "SOURCE_LAYOUT_CHANGED", "结果缺少可核验的稳定来源标识。")
        require(not dbname or re_safe(dbname), "SOURCE_LAYOUT_CHANGED", "结果数据库标识格式未知。")
        identity = f"{dbcode or dbname}:{filename}"
        target = urlsplit(url)
        if session_bound_link:
            require(target.path.rstrip("/").casefold() == "/kcms2/article/abstract",
                    "SOURCE_LAYOUT_CHANGED", "签名详情入口路径未知。")
            if detail_locators is not None:
                detail_locators[identity] = url
            detail_url = None
        else:
            stable_args = {key: value for key, value in (("dbcode", dbcode), ("dbname", dbname), ("filename", filename)) if value}
            detail_url = urlunsplit((target.scheme, target.netloc, target.path, urlencode(stable_args), ""))
        def value(selector, row=row):
            element = row.select_one(selector)
            return element.get_text(" ", strip=True) if element else None
        author_cell = row.select_one(selectors.authors)
        authors = [re.sub(r"\s+", " ", a.get_text()).strip() for a in author_cell.select("a")
                   if a.get_text(strip=True)] if author_cell else []
        if not authors:
            authors = [x.strip() for x in re.split(r"[;；,，]+", value(selectors.authors) or "") if x.strip()]
        date = value(selectors.date)
        year_match = re.search(r"\b(19|20)\d{2}\b", date or "")
        dbcode = dbcode.upper()
        paper_type = {"CJFQ": "journal", "CJFD": "journal", "CDFD": "thesis", "CMFD": "thesis",
                      "CAPJ": "journal", "CPFD": "conference"}.get(dbcode, "other")
        records.append(PaperInput(
            title=re.sub(r"\s+", " ", link.get_text()).strip(), authors=authors,
            source=value(selectors.source), year=int(year_match[0]) if year_match else None,
            source_provider="cnki", source_id=identity, detail_url=public_url(detail_url), paper_type=paper_type,
        ))
    return records


class CNKIProvider:
    """Controlled page adapter. Selectors require a real, permitted pilot acceptance."""

    def __init__(self, settings: Settings, access: BrowserAccess, gate: PolicyGate):
        self.settings, self.access, self.gate = settings, access, gate
        self.last_request: dict[str, float] = {}

    async def throttle(self, institution_id: str, *operations: str):
        interval = max(self.gate.check(institution_id, operation).min_interval_seconds for operation in operations)
        elapsed = time.monotonic() - self.last_request.get(institution_id, 0)
        delay = interval - elapsed
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request[institution_id] = time.monotonic()

    async def guard_page(self, page, profile: Institution, session_id: str):
        self.access.assert_not_blocked(session_id)
        validate_url(page.url, profile.provider_hosts)
        require(not any(x in urlsplit(page.url).path.casefold() for x in ("login", "saml", "callback")),
                "SESSION_EXPIRED", "来源已转到认证页面。")
        require(not await page.locator('input[type="password"]:visible').count(),
                "USER_INTERACTION_REQUIRED", "请在浏览器中完成认证。")
        # Fixed visible status phrases; never return complete DOM or provider exception text.
        text = await page.locator("body").inner_text()
        if any(word in text for word in ("访问过于频繁", "访问频繁", "操作频繁")):
            raise AppError("PROVIDER_RATE_LIMITED", "来源提示访问频繁，已暂停。", "按来源提示等待后重新验证。")
        if "安全验证" in await page.title() or any(word in text for word in ("请输入验证码", "安全验证", "拖动滑块")):
            raise AppError("USER_INTERACTION_REQUIRED", "来源需要验证码或安全验证。", "请在浏览器中亲自操作。")

    async def goto(self, page, url: str, profile: Institution, session_id: str):
        validate_url(url, profile.provider_hosts)
        from playwright.async_api import Error, TimeoutError

        for attempt in range(3):
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
                if response and response.status == 429:
                    raise AppError("PROVIDER_RATE_LIMITED", "来源限流，已暂停。")
                if response and response.status >= 500:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise AppError("PROVIDER_UNAVAILABLE", "来源暂时不可用。", retryable=True)
                require(not response or response.status < 400, "PROVIDER_UNAVAILABLE", "来源返回访问错误。")
                return
            except (TimeoutError, Error):
                self.access.assert_not_blocked(session_id)
                if attempt == 2:
                    raise AppError("NETWORK_NOT_READY", "网络访问失败。", "检查校园网或 VPN。", True) from None
                await asyncio.sleep(2 ** attempt)

    async def page(self, context, profile: Institution, session_id: str, query_id: str,
                   query: SearchQuery, filters: SearchFilters, sort: str, page_number: int,
                   record_limit: int | None = None):
        selectors = profile.selectors
        # Reject unsupported conditions before submitting any search; never silently drop filters.
        if query.field != "subject":
            require(query.field in selectors.search_fields, "UNSUPPORTED_QUERY", "当前档案尚未核验该检索字段。")
        controls = {k: v for k, v in filters.model_dump().items() if v not in (None, [])}
        require(all(k in selectors.filter_controls for k in controls),
                "UNSUPPORTED_FILTER", "当前入口尚未配置该筛选条件。")
        require(sort == selectors.default_sort or sort in selectors.sort_controls,
                "UNSUPPORTED_SORT", "当前入口尚未核验该排序控件或默认排序。")
        await self.throttle(profile.institution_id, "automated_search", "metadata_collect")
        if page_number == 1:
            page = await context.new_page()
            self.access.query_pages[query_id] = page
            await self.goto(page, profile.search_url, profile, session_id)
            await self.guard_page(page, profile, session_id)
            from playwright.async_api import Error

            stage = "字段菜单"
            try:
                if query.field != "subject":
                    if selectors.search_field_trigger:
                        trigger = page.locator(selectors.search_field_trigger)
                        await trigger.wait_for(state="visible")
                        await trigger.click()
                    stage = "字段选项"
                    await page.locator(selectors.search_fields[query.field]).click()
                stage = "字段标签"
                await self.verify_field(page, profile, query.field)
                stage = "筛选条件"
                for key, value in controls.items():
                    control = page.locator(selectors.filter_controls[key])
                    await control.fill(str(value) if not isinstance(value, list) else ";".join(value))
                stage = "检索框"
                await page.locator(selectors.search_input).fill(query.text)
                await self.throttle(profile.institution_id, "automated_search", "metadata_collect")
                stage = "查询提交"
                await page.locator(selectors.search_button).click()
                self.last_request[profile.institution_id] = time.monotonic()
                await page.wait_for_load_state("domcontentloaded")
            except Error:
                await self.guard_page(page, profile, session_id)
                raise AppError("SOURCE_LAYOUT_CHANGED", f"{stage}控件未按预期出现或完成操作，请更新入口适配。") from None
        else:
            page = self.access.query_pages.get(query_id)
            require(page and not page.is_closed(), "CURSOR_EXPIRED", "浏览器检索快照已丢失，请重新检索。")
            await self.guard_page(page, profile, session_id)
            previous = await page.locator(selectors.result_rows).all_text_contents()
            try:
                await page.locator(selectors.next_page).click()
                self.last_request[profile.institution_id] = time.monotonic()
                await page.wait_for_function(
                    """({selector, old}) => JSON.stringify(Array.from(document.querySelectorAll(selector),
                    row => row.textContent)) !== JSON.stringify(old)""",
                    arg={"selector": selectors.result_rows, "old": previous},
                )
            except Exception:
                await self.guard_page(page, profile, session_id)
                raise AppError("SOURCE_LAYOUT_CHANGED", "分页结果没有变化，停止采集以避免重复页。") from None
        try:
            await page.locator(f"{selectors.result_rows}, {selectors.empty_results}").first.wait_for(state="visible")
        except Exception:
            await self.guard_page(page, profile, session_id)
            raise AppError("SOURCE_LAYOUT_CHANGED", "结果等待超时或页面结构变化，停止采集。") from None
        await self.guard_page(page, profile, session_id)
        await self.verify_field(page, profile, query.field)
        if page_number == 1 and sort != selectors.default_sort:
            await self.apply_sort(page, profile, session_id, sort)
        if selectors.sort_active_class and sort in selectors.sort_controls:
            selected = page.locator(selectors.sort_controls[sort])
            require(await selected.count() and selectors.sort_active_class in (
                (await selected.first.get_attribute("class")) or "").split(),
                "SOURCE_LAYOUT_CHANGED", "来源未确认所请求的排序，停止返回结果。")
        detail_locators = {}
        html = await page.content()
        source_page_truncated = record_limit is not None and len(
            BeautifulSoup(html, "html.parser").select(selectors.result_rows)) > record_limit
        records = extract_results(html, page.url, profile, record_limit, detail_locators)
        for source_id, locator in detail_locators.items():
            self.access.detail_locators[(session_id, source_id)] = locator
        next_button = page.locator(selectors.next_page)
        has_next = bool(await next_button.count() and await next_button.first.is_visible()
                        and await next_button.first.is_enabled()
                        and "disabled" not in ((await next_button.first.get_attribute("class")) or "").lower())
        return records, has_next, source_page_truncated

    async def verify_field(self, page, profile: Institution, field: str):
        selectors = profile.selectors
        if selectors.search_field_label:
            expected = selectors.search_field_labels.get(field)
            require(expected, "UNSUPPORTED_QUERY", "该检索字段尚无核验标签。")
            label = page.locator(selectors.search_field_label)
            await label.wait_for(state="visible")
            require(await label.count() == 1 and (await label.inner_text()).strip() == expected,
                    "SOURCE_LAYOUT_CHANGED", "来源未保持所请求的检索字段，停止返回结果。")

    async def apply_sort(self, page, profile: Institution, session_id: str, sort: str):
        from playwright.async_api import Error

        selectors = profile.selectors
        control = page.locator(selectors.sort_controls[sort])
        previous = await page.locator(selectors.result_rows).all_text_contents()
        first = await page.locator(selectors.result_rows).first.element_handle() if previous else None
        await self.throttle(profile.institution_id, "automated_search", "metadata_collect")
        try:
            await control.click()
            self.last_request[profile.institution_id] = time.monotonic()
            await page.wait_for_load_state("domcontentloaded")
            if first:
                try:
                    await page.wait_for_function("""({element, selector, old}) => !element.isConnected ||
                        JSON.stringify(Array.from(document.querySelectorAll(selector), row => row.textContent)) !== JSON.stringify(old)
                    """, arg={"element": first, "selector": selectors.result_rows, "old": previous})
                except Error:
                    # A full navigation invalidates the old DOM handle. A still-connected
                    # handle means no observable result update and cannot be accepted.
                    try:
                        connected = await first.evaluate("element => element.isConnected")
                    except Error:
                        connected = False
                    if connected:
                        raise
            await page.locator(f"{selectors.result_rows}, {selectors.empty_results}").first.wait_for(state="visible")
        except Error:
            await self.guard_page(page, profile, session_id)
            raise AppError("SOURCE_LAYOUT_CHANGED", "排序结果未能可靠更新，停止返回旧排序。") from None
        finally:
            if first:
                try:
                    await first.dispose()
                except Error:
                    pass

    async def detail(self, context, profile: Institution, session_id: str, paper: dict):
        detail_url = self.detail_locator(session_id, paper)
        await self.throttle(profile.institution_id, "metadata_collect")
        page = await context.new_page()
        try:
            await self.goto(page, detail_url, profile, session_id)
            await self.guard_page(page, profile, session_id)
            title = page.locator(profile.selectors.detail_title)
            require(await title.count(), "SOURCE_LAYOUT_CHANGED", "详情缺少预期题名。")
            abstract = page.locator(profile.selectors.abstract)
            abstract_text = (await abstract.first.inner_text()).strip() if await abstract.count() else None
            if abstract_text and abstract_text.endswith(("...", "…")):
                parent = abstract.first.locator("..")
                for label in ("更多", "展开"):
                    expand = parent.get_by_text(label, exact=True)
                    if await expand.count() and await expand.first.is_visible():
                        await expand.first.click()
                        abstract_text = (await abstract.first.inner_text()).strip()
                        break
            return {"title": (await title.first.inner_text()).strip(),
                    "abstract": abstract_text,
                    "abstract_status": "partial" if abstract_text and abstract_text.endswith(("...", "…"))
                                       else "source_text" if abstract_text else "unknown"}
        finally:
            await page.close()

    async def download(self, context, profile: Institution, session_id: str, paper: dict, target):
        detail_url = self.detail_locator(session_id, paper)
        await self.throttle(profile.institution_id, "fulltext_download")
        page = await context.new_page()
        try:
            await self.goto(page, detail_url, profile, session_id)
            await self.guard_page(page, profile, session_id)
            selectors = profile.selectors
            if selectors.not_subscribed and await page.locator(selectors.not_subscribed).count():
                raise AppError("RESOURCE_NOT_SUBSCRIBED", "此资源不在当前订购范围内。")
            require(selectors.resource_available and await page.locator(selectors.resource_available).count(),
                    "ACCESS_UNDETERMINED", "没有可靠的单篇全文访问证据。", "请使用正常网页人工核验。")
            async with page.expect_download(timeout=self.settings.network_timeout_ms) as event:
                await page.locator(selectors.pdf_download).click()
            download = await event.value
            validate_url(download.url, profile.allowed_hosts)
            require(await download.failure() is None, "FILE_VALIDATION_FAILED", "浏览器下载失败。")
            await download.save_as(target)
            self.access.assert_not_blocked(session_id)
        finally:
            await page.close()

    def detail_locator(self, session_id: str, paper: dict) -> str:
        require(paper.get("source_provider") == "cnki", "SOURCE_NOT_SUPPORTED", "该版本不是知网来源。")
        locator = self.access.detail_locators.get((session_id, paper.get("source_id"))) or paper.get("detail_url")
        require(locator, "DETAIL_LINK_EXPIRED", "当前会话没有此文献的正常详情入口。",
                "在当前会话重新检索此文献，再刷新详情。")
        return locator
