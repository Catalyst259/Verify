"""Search live public-note DOM with a dedicated, manually authenticated browser.

The source implements EvidenceSource.search and can register each search/read
through a Fact execution hook. It never exports login state or writes scraped
JSONL; signed detail links exist only inside the current query.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from uuid import uuid4
import warnings

from backend.verification.models import Evidence
from backend.verification.subgraphs.facts.model import FactEvidence
from backend.common.errors import LinkReadError
from backend.common.xiaohongshu_links import allowed_navigation, canonical_note_id, valid_note_url
from backend.extraction.materials import LinkMaterial


HOME = "https://www.xiaohongshu.com/explore"
logger = logging.getLogger(__name__)
_NETWORK_ERRORS = {
    "net::ERR_ABORTED", "net::ERR_TIMED_OUT", "net::ERR_NETWORK_CHANGED",
    "net::ERR_NAME_NOT_RESOLVED", "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_RESET", "net::ERR_CONNECTION_REFUSED",
    "net::ERR_PROXY_CONNECTION_FAILED", "net::ERR_TUNNEL_CONNECTION_FAILED",
    "net::ERR_SSL_PROTOCOL_ERROR", "net::ERR_CERT_AUTHORITY_INVALID", "net::ERR_CERT_DATE_INVALID",
    "net::ERR_BLOCKED_BY_CLIENT", "net::ERR_HTTP_RESPONSE_CODE_FAILURE", "net::ERR_EMPTY_RESPONSE",
}
Execute = Callable[..., Awaitable[dict]]
BodyCache = dict[str, tuple[dict, datetime]]

# Read visible cards and detail text, never application state or private APIs.
CARDS_JS = r"""() => Array.from(document.querySelectorAll('.note-item')).filter(card =>
    card.getClientRects().length > 0 && getComputedStyle(card).visibility !== 'hidden'
).map(card => {
    const link = card.querySelector('a.cover[href]') || card.querySelector('a.title[href]');
    const author = card.querySelector('a.author');
    return {
        url: link?.href || '', title: card.querySelector('.title')?.innerText?.trim() || '',
        author: card.querySelector('.author .name')?.innerText?.trim() || author?.innerText?.trim() || ''
    };
}).filter(row => row.url)"""

DETAIL_JS = r"""() => {
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const text = selectors => {
        for (const s of selectors) {
            const e = Array.from(document.querySelectorAll(s)).find(visible);
            if (e) return e.innerText.trim();
        }
        return '';
    };
    return {
        title: text(['#detail-title']), content: text(['#detail-desc']),
        author: text(['.note-container .author .username', '.author-container .username']),
        publish_time: text(['.note-content .date', '.bottom-container .date']),
        detail_found: Array.from(document.querySelectorAll('#detail-desc')).some(visible)
    };
}"""

GUARD_JS = r"""() => {
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    if (Array.from(document.querySelectorAll('.login-container, .login-modal')).some(visible)) return 'login';
    if (Array.from(document.querySelectorAll('iframe')).some(e => visible(e) && /captcha|verify/i.test(e.src))) return 'verify';
    const messages = Array.from(document.querySelectorAll('.reds-modal-open, [role="dialog"], [role="alert"], .error-page, .error-container')).filter(visible).map(e => e.innerText);
    if (!Array.from(document.querySelectorAll('.note-item, #detail-desc')).some(visible)) messages.push(document.body?.innerText || '');
    if (messages.some(t => /访问过于频繁|操作过于频繁|请求过于频繁|访问频次异常|安全验证|请完成验证|拖动滑块|网络环境存在风险|异常访问/.test(t))) return 'verify';
    const input = document.querySelector('#search-input');
    if (visible(input) && /登录/.test(input.getAttribute('placeholder') || '')) return 'login';
    return '';
}"""

EMPTY_JS = r"""() => Array.from(document.querySelectorAll('.no-result, .no-results, .empty-container, .empty')).some(e =>
    e.getClientRects().length > 0 && /暂无.*笔记|没有.*结果|没有.*笔记|未找到/.test(e.innerText))"""

HOME_READY_JS = r"""() => {
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    return visible(document.querySelector('#search-input')) || visible(document.querySelector('#search-input-in-feeds')) ||
        Array.from(document.querySelectorAll('.note-item')).some(visible);
}"""


class XiaohongshuError(RuntimeError):
    """A controlled source failure; previously registered evidence remains usable."""

    partial_evidence: list[Evidence]

    def __init__(self, message: str):
        super().__init__(message)
        self.partial_evidence = []


class XiaohongshuLoginRequired(XiaohongshuError):
    pass


class XiaohongshuAccessRestricted(XiaohongshuError):
    pass


class XiaohongshuTimeout(XiaohongshuError):
    pass


def _published_at(value: str) -> date | datetime | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


class XiaohongshuSource:
    """One async owner per dedicated profile, shared by the application's requests.

    Queries include lock waiting in their timeout. The account must be logged
    in separately; background queries never open a visible login window.
    """

    def __init__(self, profile_path: Path, *, max_results: int = 10, timeout_seconds: float = 120,
                 executable_path: str | None = None, pacing_seconds: float = 4):
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10:
            raise ValueError("xiaohongshu.max_results 必须为 1–10 的整数")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("xiaohongshu.timeout_seconds 必须大于 0")
        if not math.isfinite(pacing_seconds) or pacing_seconds < 0:
            raise ValueError("xiaohongshu.pacing_seconds 不能为负数")
        self.profile_path = Path(profile_path).resolve()
        self.max_results = max_results
        self.timeout_seconds = timeout_seconds
        self.executable_path = executable_path or None
        self.pacing_seconds = pacing_seconds
        self._last_detail_navigation = perf_counter()
        self._lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._owner_task = None
        self._closed = False

    @classmethod
    def from_config(cls, config: dict, *, root: Path, data_directory: Path) -> "XiaohongshuSource":
        settings = config.get("xiaohongshu", {})
        profile = Path(settings.get("profile_path") or data_directory / "xiaohongshu-profile")
        return cls(profile if profile.is_absolute() else root / profile,
                   max_results=settings.get("max_results", 10), timeout_seconds=settings.get("timeout_seconds", 120),
                   executable_path=settings.get("executable_path") or None,
                   pacing_seconds=settings.get("pacing_seconds", 4))

    def for_run(self) -> "_RunSource":
        """Share validated bodies within one verification, keeping the profile lock shared."""
        return _RunSource(self)

    async def _ensure_context(self, *, headless: bool = True):
        if self._context is not None:
            return self._context
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            options = {"headless": headless, "locale": "zh-CN", "viewport": {"width": 1280, "height": 820},
                       "accept_downloads": False, "timeout": 25000}
            if self.executable_path:
                options["executable_path"] = self.executable_path
            else:
                options["channel"] = "msedge"
            self._context = await self._playwright.chromium.launch_persistent_context(str(self.profile_path), **options)
            self._context.set_default_timeout(8000)
            return self._context
        except asyncio.CancelledError:
            raise
        except Exception:
            raise XiaohongshuError("无法启动专用小红书浏览器；检查系统 Edge/配置路径，并关闭占用该登录资料的浏览器") from None

    async def _reset_browser(self):
        context, playwright = self._context, self._playwright
        self._context = self._playwright = None

        async def dispose():
            for handle, method in ((context, "close"), (playwright, "stop")):
                if handle is None:
                    continue
                try:
                    await asyncio.wait_for(getattr(handle, method)(), timeout=2)
                except Exception:
                    warnings.warn("小红书浏览器清理未在限定时间内完成", RuntimeWarning, stacklevel=2)

        task = asyncio.create_task(dispose())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _goto(self, page, url: str):
        from playwright.async_api import TimeoutError as NavigationTimeout

        note_navigation = valid_note_url(url)
        if note_navigation:
            while (delay := self._last_detail_navigation + self.pacing_seconds - perf_counter()) > 0:
                await asyncio.sleep(delay)
        started = perf_counter()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Browser exceptions may contain signed links; export only fixed error metadata.
            first_line = str(error).splitlines()[0] if str(error) else ""
            match = re.match(r"^Page\.goto:\s+(net::ERR_[A-Z_]+)\s+at\s+https?://", first_line)
            code = match[1] if match and match[1] in _NETWORK_ERRORS else None
            expired = isinstance(error, (NavigationTimeout, TimeoutError))
            logger.warning("xiaohongshu_navigation_failed %s", json.dumps({
                "host": urlsplit(url).hostname,
                "page": "detail" if canonical_note_id(url) else "search" if urlsplit(url).path.rstrip("/") == "/search_result" else "home",
                "error_type": type(error).__name__, "category": "timeout" if expired else "navigation",
                "network_error": code,
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            }))
            if expired:
                raise XiaohongshuTimeout("小红书网页导航超时：25秒内未完成 DOM 加载") from None
            detail = code or type(error).__name__
            raise XiaohongshuError(f"小红书网页导航失败（{detail}）") from None
        finally:
            if note_navigation:
                # Include redirects and failed/cancelled attempts conservatively;
                # waiting cancellation above has not issued a navigation.
                self._last_detail_navigation = perf_counter()
        if response is not None and response.status in (401, 403, 429):
            raise XiaohongshuAccessRestricted(f"小红书网页限制访问（HTTP {response.status}）")
        if response is not None and response.status >= 400:
            raise XiaohongshuError(f"小红书网页返回 HTTP {response.status}，未采用错误页面正文")

    async def _evaluate(self, page, script: str):
        try:
            return await page.evaluate(script)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise XiaohongshuError("小红书可见页面结构读取失败") from None

    async def _guard(self, page):
        parts = urlsplit(page.url)
        if (parts.scheme == "https" and parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"}
                and parts.path.rstrip("/") == "/website-login/captcha"):
            raise XiaohongshuAccessRestricted("小红书进入验证码验证页；后台采集已停止，请单独检查登录浏览器")
        reason = await self._evaluate(page, GUARD_JS)
        if reason == "login":
            raise XiaohongshuLoginRequired("小红书需要重新登录；请单独运行 python -m backend.sources.xiaohongshu --login")
        if reason == "verify":
            raise XiaohongshuAccessRestricted("小红书要求验证或限制访问；后台采集已停止，请单独检查登录浏览器")

    @staticmethod
    def _is_search(page, query: str) -> bool:
        try:
            parts = urlsplit(page.url)
            keywords = parse_qs(parts.query, keep_blank_values=True).get("keyword", [])
            path = parts.path.rstrip("/")
            return (parts.scheme == "https" and parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"}
                    and ((path == "/search_result" and keywords == [query]) or
                         (path == "/search_result_ai" and len(keywords) == 1 and unquote(keywords[0]) == query)))
        except ValueError:
            return False

    @staticmethod
    def _identity_metadata(page, query: str) -> dict:
        """Export only route categories and keyword equality, never raw signed links or terms."""
        try:
            parts = urlsplit(page.url)
            keywords = parse_qs(parts.query, keep_blank_values=True).get("keyword", [])
            path = parts.path.rstrip("/")
            kind = "search" if path in {"/search_result", "/search_result_ai"} else "home" if path == "/explore" else (
                "note" if canonical_note_id(page.url) else "other")
            return {"scheme_https": parts.scheme == "https",
                    "host_allowed": parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"},
                    "path_kind": kind, "keyword_present": bool(keywords), "keyword_exact": keywords == [query],
                    "keyword_single_decode_exact": len(keywords) == 1 and unquote(keywords[0]) == query,
                    "keyword_whitespace_equal": len(keywords) == 1 and " ".join(keywords[0].split()) == " ".join(query.split())}
        except ValueError:
            return {"scheme_https": False, "host_allowed": False, "path_kind": "invalid",
                    "keyword_present": False, "keyword_exact": False, "keyword_single_decode_exact": False,
                    "keyword_whitespace_equal": False}

    async def _wait_ready(self, page, query: str, *, home: bool = False):
        # DOMContentLoaded precedes SPA initialization; home cards indicate readiness only,
        # and are never read as requested-keyword candidates.
        for _ in range(20):
            await self._guard(page)
            identity = self._identity_metadata(page, query)
            if not identity["scheme_https"] or not identity["host_allowed"]:
                break
            ready = (identity["path_kind"] == "home" and await self._evaluate(page, HOME_READY_JS)) if home else self._is_search(page, query)
            if ready:
                return
            await asyncio.sleep(.25)
        logger.warning("xiaohongshu_identity_failed %s", json.dumps({
            "phase": "home_ready" if home else "search_ready", **self._identity_metadata(page, query),
        }))
        if home:
            raise XiaohongshuError("小红书首页搜索控件未就绪，未继续读取候选")
        raise XiaohongshuError("未进入对应关键词的小红书搜索页；等待页面就绪后仍未匹配")

    async def _enter_search(self, page, query: str):
        """Use the site's visible search controls; direct search URLs can trigger its gate."""
        from playwright.async_api import TimeoutError as ActionTimeout

        def require_home():
            identity = self._identity_metadata(page, query)
            if not identity["scheme_https"] or not identity["host_allowed"] or identity["path_kind"] != "home":
                raise XiaohongshuError("小红书搜索输入页已切换，未继续提交关键词")

        try:
            await self._guard(page)
            require_home()
            search_input = page.locator("#search-input").first
            if not await search_input.is_visible():
                search_input = page.locator("#search-input-in-feeds").first
                if not await search_input.is_visible():
                    raise XiaohongshuError("小红书搜索输入框未显示，未尝试填写隐藏控件")
                # Focusing the current feed control mounts a second textarea; follow
                # only the actual visible focused editor inside the observed wrapper.
                await search_input.click(timeout=5000)
                await self._guard(page)
                require_home()
                search_input = page.locator("textarea:focus")
                if (await search_input.count() != 1 or not await search_input.is_visible() or
                        not await search_input.evaluate("e => e.tagName === 'TEXTAREA' && !!e.closest('.wendian-wrapper')")):
                    raise XiaohongshuError("小红书活动搜索编辑框未就绪，未尝试填写其他控件")
            await self._guard(page)
            require_home()
            await search_input.fill(query, timeout=5000)
            await self._guard(page)
            require_home()
            await search_input.press("Enter", timeout=5000)
        except (asyncio.CancelledError, XiaohongshuError):
            raise
        except (ActionTimeout, TimeoutError):
            raise XiaohongshuTimeout("小红书可见搜索控件操作超时，未读取候选") from None
        except Exception:
            # Playwright action exceptions can contain submitted text or signed URLs.
            raise XiaohongshuError("小红书可见搜索控件操作失败，未读取候选") from None

    async def _candidates(self, page, query: str, excluded: frozenset[str], private: dict,
                          limit: int | None = None) -> list[dict]:
        # Initialize the logged-in site, then let its native search events create
        # the requested route. Home recommendations never become query materials.
        await self._goto(page, HOME)
        await self._wait_ready(page, query, home=True)
        await self._enter_search(page, query)
        await self._wait_ready(page, query)
        for _ in range(20):
            await self._guard(page)
            if not self._is_search(page, query):
                raise XiaohongshuError("未进入对应关键词的小红书搜索页")
            cards = await self._evaluate(page, CARDS_JS)
            if cards:
                break
            if await self._evaluate(page, EMPTY_JS):
                return []
            await asyncio.sleep(0.25)
        else:
            raise XiaohongshuError("小红书搜索页面没有可识别的笔记结果")
        results, idle_rounds = {}, 0
        for _ in range(8):
            await self._guard(page)
            if not self._is_search(page, query):
                raise XiaohongshuError("小红书搜索页面已切换")
            before = len(results)
            for card in cards:
                identity = canonical_note_id(card.get("url"))
                if not identity or identity in excluded or urlsplit(card["url"]).scheme != "https":
                    continue
                private[identity] = card
                results[identity] = {"note_id": identity, "title": card.get("title", ""),
                                     "url": f"https://www.xiaohongshu.com/explore/{identity}"}
                if len(results) >= (limit or self.max_results):
                    return list(results.values())
            idle_rounds = idle_rounds + 1 if len(results) == before else 0
            if idle_rounds >= 2:
                break
            await page.mouse.wheel(0, 950)
            await asyncio.sleep(min(3, self.pacing_seconds))
            cards = await self._evaluate(page, CARDS_JS)
        return list(results.values())

    async def _read(self, page, identity: str, card: dict) -> dict:
        await self._goto(page, card["url"])
        previous = None
        for _ in range(16):
            await self._guard(page)
            if canonical_note_id(page.url) != identity:
                raise XiaohongshuError("小红书详情跳转至其他笔记或不可识别页面")
            data = await self._evaluate(page, DETAIL_JS)
            if data["detail_found"] and data["content"].strip():
                stable = tuple(data.get(key, "") for key in ("title", "content", "author"))
                if stable == previous:
                    title = data["title"] or card.get("title", "")
                    author = data["author"] or card.get("author", "")
                    return {"source": f"小红书 · {author}" if author else "小红书", "source_type": "WEB",
                            "content": f"{title}\n\n{data['content']}" if title else data["content"],
                            "url": f"https://www.xiaohongshu.com/explore/{identity}",
                            "published_at": _published_at(data["publish_time"])}
                previous = stable
            await asyncio.sleep(0.5)
        raise XiaohongshuError("小红书详情正文未显示或未稳定，未将候选摘要作为证据")

    async def _note_images(self, page, identity: str) -> tuple[bytes, ...]:
        """逐张切换真实轮播图并截取可见图片；不读取应用状态或评论区图片。"""
        from playwright.async_api import TimeoutError as BrowserTimeout

        slider = page.locator('.note-container .note-slider').first
        if await page.locator('.note-container video').count():
            raise LinkReadError("暂不支持视频笔记，请提交图文笔记或截图", 422)
        if not await slider.count():
            # 发现媒体容器却无法识别轮播时不能把笔记当作无配图处理。
            if await page.locator('.note-container .xhs-slider-container').count():
                raise LinkReadError("笔记配图结构无法识别，未忽略配图继续核验")
            return ()
        indices = await slider.locator('.swiper-slide').evaluate_all(
            "els => [...new Set(els.map(e => e.getAttribute('data-swiper-slide-index') ?? e.getAttribute('data-index')))]")
        if not indices or any(index is None or not index.isdecimal() for index in indices):
            raise LinkReadError("笔记配图序号无法识别")
        indices = sorted({int(index) for index in indices})
        if indices != list(range(len(indices))):
            raise LinkReadError("笔记配图序号不完整")
        active = slider.locator('.swiper-slide-active')

        async def active_index():
            await active.wait_for(state='visible', timeout=8000)
            value = await active.get_attribute('data-swiper-slide-index')
            return int(value if value is not None else await active.get_attribute('data-index'))

        async def move(direction, expected):
            button = page.locator(f'.note-container .slider-zoom-in .arrow-controller.{direction}').first
            if not await button.count():
                button = page.locator(f'.note-container .arrow-controller.{direction}').first
            await button.click(timeout=8000)
            await page.wait_for_function("""expected => {
                const e = document.querySelector('.note-container .note-slider .swiper-slide-active');
                return e && Number(e.getAttribute('data-swiper-slide-index') ?? e.getAttribute('data-index')) === expected;
            }""", arg=expected, timeout=8000)

        images = []
        index = 0
        try:
            initial = await active_index()
            if initial not in indices:
                raise LinkReadError("笔记当前配图序号无效")
            for previous in range(initial - 1, -1, -1):
                await move('left', previous)
            for index in indices:
                await self._guard(page)
                if canonical_note_id(page.url) != identity:
                    raise LinkReadError("读取配图时页面切换到其他笔记")
                if index:
                    await move('right', index)
                picture = active.locator('img').first
                await picture.wait_for(state='visible', timeout=8000)
                await page.wait_for_function("""() => {
                    const e = document.querySelector('.note-container .note-slider .swiper-slide-active img');
                    return e && e.complete && e.naturalWidth >= 32 && e.naturalHeight >= 32;
                }""", timeout=8000)
                if await active.locator('video').count():
                    raise LinkReadError("暂不支持包含视频的笔记，请提交图文笔记或截图", 422)
                images.append(await picture.screenshot(type='png', animations='disabled', timeout=8000,
                    style='.arrow-controller, .fraction, .slider-pagination-container { visibility: hidden !important; }'))
            await self._guard(page)
            if canonical_note_id(page.url) != identity:
                raise LinkReadError("读取配图时页面切换到其他笔记")
        except BrowserTimeout:
            raise LinkReadError(f"第 {index + 1} 张配图加载或切换超时，未忽略该图继续核验", 504) from None
        return tuple(images)

    async def _fetch_note_document(self, route) -> tuple[int, dict, bytes]:
        """不自动跟随 HTTP 跳转，避免浏览器路由只拦截跳转链首个请求。"""
        response = await route.fetch(max_redirects=0, timeout=25000)
        try:
            return response.status, response.headers, await response.body()
        finally:
            await response.dispose()

    async def read_note(self, url: str, *, deadline_at: datetime | None = None,
                        _note_cache: dict[str, LinkMaterial] | None = None) -> LinkMaterial:
        """读取用户输入笔记，含锁等待的超时；原材料缓存仅由单次运行持有。"""
        if not valid_note_url(url):
            raise LinkReadError("必须是小红书笔记链接或 xhslink.com 分享短链", 422)
        if deadline_at is not None and (deadline_at.tzinfo is None or deadline_at.utcoffset() is None):
            raise ValueError("小红书读取截止时间必须带时区")
        expected = canonical_note_id(url)
        if _note_cache is not None and expected in _note_cache:
            return replace(_note_cache[expected], original_url=url)
        timeout = min(self.timeout_seconds, (deadline_at - datetime.now(timezone.utc)).total_seconds()
                      if deadline_at is not None else self.timeout_seconds)
        started = perf_counter()
        try:
            async with asyncio.timeout(max(0, timeout)):
                async with self._lock:
                    if self._closed:
                        raise LinkReadError("小红书来源已关闭", 503)
                    self._owner_task = asyncio.current_task()
                    page = None
                    blocked = False
                    navigations = 0
                    navigation_error = None
                    try:
                        context = await self._ensure_context()
                        page = await context.new_page()

                        async def restrict_navigation(route):
                            nonlocal blocked, navigations, navigation_error
                            request = route.request
                            if request.is_navigation_request() and request.frame == page.main_frame:
                                navigations += 1
                                if navigations > 8 or not allowed_navigation(request.url):
                                    blocked = True
                                    await route.abort()
                                    return
                                try:
                                    status, headers, body = await self._fetch_note_document(route)
                                    if status in {301, 302, 303, 307, 308}:
                                        target = urljoin(request.url, headers.get('location', ''))
                                        if not headers.get('location') or not allowed_navigation(target):
                                            blocked = True
                                            await route.abort()
                                            return
                                        # 用新的文档导航继续合法跳转，每一跳都会重新经过域名校验。
                                        literal = json.dumps(target).replace('<', '\\u003c')
                                        await route.fulfill(status=200, content_type='text/html',
                                            body=f'<script>location.replace({literal})</script>')
                                    else:
                                        headers = {key: value for key, value in headers.items()
                                                   if key.lower() not in {'content-encoding', 'content-length', 'transfer-encoding'}}
                                        await route.fulfill(status=status, headers=headers, body=body)
                                except Exception as error:
                                    from playwright.async_api import TimeoutError as BrowserTimeout
                                    navigation_error = LinkReadError('小红书页面请求超时', 504) if isinstance(
                                        error, (TimeoutError, BrowserTimeout)) else LinkReadError('小红书页面请求失败')
                                    await route.abort()
                                return
                            await route.fallback()

                        await page.route('**/*', restrict_navigation)
                        await self._goto(page, HOME)
                        await self._guard(page)
                        await self._goto(page, url)
                        previous = None
                        identity = None
                        for _ in range(20):
                            await self._guard(page)
                            identity = canonical_note_id(page.url)
                            if identity:
                                if expected and identity != expected:
                                    raise LinkReadError("链接跳转到了另一篇笔记")
                                if _note_cache is not None and identity in _note_cache:
                                    return replace(_note_cache[identity], original_url=url)
                                data = await self._evaluate(page, DETAIL_JS)
                                if data['detail_found'] or await page.locator('.note-container .note-slider').count():
                                    stable = (data['title'], data['content'])
                                    if previous == stable:
                                        break
                                    previous = stable
                            await asyncio.sleep(0.5)
                        else:
                            raise LinkReadError("笔记已失效、非笔记页面或正文未加载完成")
                        images = await self._note_images(page, identity)
                        if not data['title'].strip() and not data['content'].strip() and not images:
                            raise LinkReadError("笔记没有可读取的标题、正文或配图")
                        material = LinkMaterial(url, f'https://www.xiaohongshu.com/explore/{identity}',
                                                data['title'], data['content'], images)
                        if _note_cache is not None:
                            _note_cache[identity] = material
                        logger.info("xiaohongshu_input_read note_id=%s images=%d elapsed_ms=%.1f",
                                    identity, len(images), (perf_counter() - started) * 1000)
                        return material
                    except asyncio.CancelledError:
                        await self._reset_browser()
                        raise
                    except Exception:
                        await self._reset_browser()
                        if blocked:
                            raise LinkReadError("分享链接跳转到非小红书网站或跳转次数过多", 422) from None
                        if navigation_error:
                            raise navigation_error from None
                        raise
                    finally:
                        try:
                            if page is not None and self._context is not None:
                                await asyncio.wait_for(page.close(), timeout=1)
                        except asyncio.CancelledError:
                            await self._reset_browser()
                            raise
                        except Exception:
                            await self._reset_browser()
                        finally:
                            self._owner_task = None
        except (XiaohongshuLoginRequired, XiaohongshuAccessRestricted) as error:
            raise LinkReadError(str(error), 503) from None
        except (TimeoutError, XiaohongshuTimeout):
            raise LinkReadError("读取超时（含等待浏览器会话），请减少材料后重试", 504) from None
        except XiaohongshuError as error:
            raise LinkReadError(str(error)) from None
        except (LinkReadError, asyncio.CancelledError):
            raise
        except Exception:
            # Playwright 异常可能携带签名 URL，只导出受控说明。
            raise LinkReadError("笔记图文读取失败，页面结构可能已变化") from None

    async def _invoke(self, operation: Callable[[], Awaitable], *, query: bool, execute: Execute | None,
                      retrieved_at: datetime | None = None) -> dict:
        if execute is None:
            data = await operation()
            if not query:
                data = FactEvidence.model_validate(data | {"evidence_id": uuid4().hex,
                    "retrieved_at": retrieved_at or datetime.now(timezone.utc)}).model_dump()
            return {"data": data}
        # Only reused bodies need an explicit timestamp; existing uncached hooks keep their signature.
        timing = {"retrieved_at": retrieved_at} if retrieved_at is not None else {}
        result = await execute(operation, query=query, **timing)
        if result.get("error"):
            error = str(result["error"])
            if "XiaohongshuLoginRequired" in error:
                raise XiaohongshuLoginRequired("小红书需要重新登录；请单独运行登录命令")
            if "XiaohongshuAccessRestricted" in error:
                raise XiaohongshuAccessRestricted("小红书验证或访问限制阻止了后台采集")
            raise XiaohongshuError("小红书搜索或正文读取失败；已登记的材料保留")
        return result

    async def search(self, query: str, *, execute: Execute | None = None,
                     excluded_ids: frozenset[str] = frozenset(), deadline_at: datetime | None = None,
                     result_limit: int | None = None, _body_cache: BodyCache | None = None,
                     _access_failures: list[XiaohongshuError] | None = None) -> list[Evidence]:
        """Search and read fresh bodies, registering candidates and each body separately.

        The timeout covers profile-lock waiting and all reads. On failure a
        controlled exception exposes partial_evidence; registered Fact items
        remain in the caller's ledger. Cancellation always propagates.
        """
        query = query.strip()
        if not query or len(query) > 100:
            raise ValueError("小红书搜索关键词须为 1–100 个字符")
        if result_limit is not None and (isinstance(result_limit, bool) or not isinstance(result_limit, int)
                                         or not 1 <= result_limit <= 10):
            raise ValueError("小红书单次读取上限须为 1–10 的整数")
        if deadline_at is not None and (deadline_at.tzinfo is None or deadline_at.utcoffset() is None):
            raise ValueError("小红书查询截止时间必须带时区")
        timeout = min(self.timeout_seconds, (deadline_at - datetime.now(timezone.utc)).total_seconds()
                      if deadline_at is not None else self.timeout_seconds)
        evidence = []
        try:
            async with asyncio.timeout(max(0, timeout)):
                async with self._lock:
                    if self._closed:
                        raise XiaohongshuError("小红书来源已关闭")
                    if _access_failures:
                        raise type(_access_failures[0])(str(_access_failures[0])) from None
                    self._owner_task = asyncio.current_task()
                    pages = []
                    query_pages_ready = False
                    try:
                        context = await self._ensure_context()
                        search_page = await context.new_page()
                        pages.append(search_page)
                        detail_page = await context.new_page()
                        pages.append(detail_page)
                        query_pages_ready = True
                        private = {}
                        result = await self._invoke(lambda: self._candidates(search_page, query,
                            frozenset(identity.lower() for identity in excluded_ids), private,
                            min(result_limit or self.max_results, self.max_results)), query=True, execute=execute)
                        if not result.get("stopped"):
                            for candidate in result["data"][:self.max_results]:
                                identity = candidate["note_id"]
                                cached = _body_cache.get(identity) if _body_cache is not None else None

                                async def read_body():
                                    if cached is not None:
                                        return dict(cached[0])
                                    return await self._read(detail_page, identity, private[identity])

                                result = await self._invoke(read_body, query=False, execute=execute,
                                                            retrieved_at=cached[1] if cached else None)
                                if result.get("stopped"):
                                    break
                                if result.get("data", {}).get("duplicate"):
                                    continue
                                item = FactEvidence.model_validate(result["data"])
                                if canonical_note_id(item.url) != identity:
                                    raise XiaohongshuError("已登记的小红书正文与候选笔记不一致")
                                if _body_cache is not None and cached is None:
                                    _body_cache[identity] = (
                                        item.model_dump(exclude={"evidence_id", "retrieved_at"}), item.retrieved_at,
                                    )
                                evidence.append(item)
                    except asyncio.CancelledError:
                        # Closing query pages stops navigation while preserving the logged-in browser
                        # for the next claim. Interrupted page/browser creation needs a full reset.
                        if not query_pages_ready:
                            await self._reset_browser()
                        raise
                    except Exception:
                        await self._reset_browser()
                        raise
                    finally:
                        try:
                            if self._context is not None:
                                for page in pages:
                                    await asyncio.wait_for(page.close(), timeout=1)
                        except asyncio.CancelledError:
                            await self._reset_browser()
                            raise
                        except Exception:
                            await self._reset_browser()
                            raise XiaohongshuError("小红书查询页面清理失败，浏览器已重置") from None
                        finally:
                            self._owner_task = None
        except TimeoutError:
            error = XiaohongshuError("小红书查询超过限定时间（含等待浏览器资料锁）")
            error.partial_evidence = evidence
            raise error from None
        except XiaohongshuError as error:
            error.partial_evidence = evidence
            if (isinstance(error, (XiaohongshuLoginRequired, XiaohongshuAccessRestricted))
                    and _access_failures is not None and not _access_failures):
                # Share only the terminal gate within this run, not another Claim's evidence.
                _access_failures.append(type(error)(str(error)))
            raise
        return evidence

    async def aclose(self):
        """Stop active work and close owned browser resources with bounded waits."""
        self._closed = True
        owner = self._owner_task
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            owner.cancel()
            await asyncio.wait({owner}, timeout=5)
        async with asyncio.timeout(5):
            async with self._lock:
                await self._reset_browser()

    async def login(self):
        """Explicit interactive setup; background search never invokes this method."""
        try:
            async with self._lock:
                context = await self._ensure_context(headless=False)
                page = await context.new_page()
                await self._goto(page, HOME)
                input("请在浏览器中手动登录/完成验证。完成后回到终端按 Enter 关闭浏览器：")
                await self._guard(page)
        finally:
            await self.aclose()


class _RunSource:
    """One run's actual bodies, Claim aliases and terminal gate; physical queries stay guarded."""

    def __init__(self, source: XiaohongshuSource):
        self._source = source
        self._body_cache: BodyCache = {}
        self._claim_notes: dict[str, str] = {}
        self._note_cache: dict[str, LinkMaterial] = {}
        self._input_aliases: dict[str, LinkMaterial] = {}
        self._access_failures: list[XiaohongshuError] = []

    async def read_note(self, url: str, *, deadline_at: datetime | None = None) -> LinkMaterial:
        if url not in self._input_aliases:
            self._input_aliases[url] = await self._source.read_note(
                url, deadline_at=deadline_at, _note_cache=self._note_cache)
        return self._input_aliases[url]

    async def search(self, query: str, *, execute: Execute | None = None,
                     excluded_ids: frozenset[str] = frozenset(), deadline_at: datetime | None = None,
                     result_limit: int | None = None, claim_id: str | None = None) -> list[Evidence]:
        alias_claim = claim_id if type(result_limit) is int and result_limit == 1 else None
        identity = self._claim_notes.get(alias_claim) if alias_claim else None
        cached = self._body_cache.get(identity)
        if cached is not None and identity not in {value.lower() for value in excluded_ids}:
            # Cached reads keep the physical source's public input and absolute-time contract.
            if not query.strip() or len(query.strip()) > 100:
                raise ValueError("小红书搜索关键词须为 1–100 个字符")
            if result_limit is not None and (isinstance(result_limit, bool) or not isinstance(result_limit, int)
                                             or not 1 <= result_limit <= 10):
                raise ValueError("小红书单次读取上限须为 1–10 的整数")
            if deadline_at is not None and (deadline_at.tzinfo is None or deadline_at.utcoffset() is None):
                raise ValueError("小红书查询截止时间必须带时区")
            timeout = min(self._source.timeout_seconds, (deadline_at - datetime.now(timezone.utc)).total_seconds()
                          if deadline_at is not None else self._source.timeout_seconds)
            try:
                async with asyncio.timeout(max(0, timeout)):
                    async with self._source._lock:
                        if self._source._closed:
                            raise XiaohongshuError("小红书来源已关闭")
                        if self._access_failures:
                            raise type(self._access_failures[0])(str(self._access_failures[0])) from None
                        if timeout <= 0 or (deadline_at is not None and datetime.now(timezone.utc) >= deadline_at):
                            raise TimeoutError
                        self._source._owner_task = asyncio.current_task()
                        try:
                            async def read_body():
                                return dict(cached[0])
                            result = await self._source._invoke(read_body, query=False, execute=execute,
                                                                retrieved_at=cached[1])
                            data = result.get("data")
                            if result.get("stopped") or not data or data.get("duplicate"):
                                return []
                            item = FactEvidence.model_validate(data)
                            if (canonical_note_id(item.url) != identity or item.retrieved_at != cached[1]
                                    or item.model_dump(exclude={"evidence_id", "retrieved_at"}) != cached[0]):
                                raise XiaohongshuError("已登记的缓存正文与实际小红书材料不一致")
                            return [item]
                        finally:
                            self._source._owner_task = None
            except TimeoutError:
                raise XiaohongshuError("小红书缓存正文登记超过限定时间") from None
        registered = set()

        async def track_read(operation, *, query=False, **kwargs):
            result = (await self._source._invoke(operation, query=query, execute=None, **kwargs)
                      if execute is None else await execute(operation, query=query, **kwargs))
            data = result.get("data")
            if not query and not result.get("stopped") and not result.get("error") and data and not data.get("duplicate"):
                identity = data.get("evidence_id")
                if isinstance(identity, str) and identity.strip():
                    registered.add(identity)
            return result

        evidence = await self._source.search(query, execute=track_read if alias_claim else execute,
                                            excluded_ids=excluded_ids,
                                            deadline_at=deadline_at, result_limit=result_limit,
                                            _body_cache=self._body_cache, _access_failures=self._access_failures)
        if alias_claim:
            for item in evidence:
                identity = canonical_note_id(item.url)
                cached = self._body_cache.get(identity)
                if (cached is not None and item.evidence_id in registered
                        and item.retrieved_at == cached[1]
                        and item.model_dump(exclude={"evidence_id", "retrieved_at"}) == cached[0]):
                    self._claim_notes[alias_claim] = identity
                    break
        return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单独设置小红书登录资料；后台搜索使用同一专用 profile")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args(argv)
    if not args.login:
        parser.error("请使用 --login；在线搜索由后端模块调用")
    from backend.extraction.agent import read_config

    root = Path(__file__).resolve().parents[2]
    source = XiaohongshuSource.from_config(read_config(), root=root, data_directory=root / "backend/data")
    try:
        asyncio.run(source.login())
    except (XiaohongshuError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("浏览器已关闭，专用登录资料已保留；登录有效性由下一次后台查询检测。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
