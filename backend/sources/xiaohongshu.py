"""Search live public-note DOM with a dedicated, manually authenticated browser.

The source implements EvidenceSource.search and can register each search/read
through a Fact execution hook. It never exports login state or writes scraped
JSONL; signed detail links exist only inside the current query.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
import warnings

from backend.verification.models import Evidence
from backend.verification.subgraphs.facts.model import FactEvidence


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
_NOTE_PATH = re.compile(r"^/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{24})/?$")
Execute = Callable[..., Awaitable[dict]]

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


def canonical_note_id(url: str) -> str | None:
    """Treat equivalent HTTP(S) input links as one ID, ignoring signed query tokens."""
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"}
                or parts.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, {"http": 80, "https": 443}.get(parts.scheme)}):
            return None
    except ValueError:
        return None
    match = _NOTE_PATH.fullmatch(parts.path)
    return match[1].lower() if match else None


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
                raise XiaohongshuError("小红书网页导航超时：25秒内未完成 DOM 加载") from None
            detail = code or type(error).__name__
            raise XiaohongshuError(f"小红书网页导航失败（{detail}）") from None
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
        reason = await self._evaluate(page, GUARD_JS)
        if reason == "login":
            raise XiaohongshuLoginRequired("小红书需要重新登录；请单独运行 python -m backend.sources.xiaohongshu --login")
        if reason == "verify":
            raise XiaohongshuAccessRestricted("小红书要求验证或限制访问；后台采集已停止，请单独检查登录浏览器")

    @staticmethod
    def _is_search(page, query: str) -> bool:
        try:
            parts = urlsplit(page.url)
            return (parts.scheme == "https" and parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"}
                    and parts.path.rstrip("/") == "/search_result" and parse_qs(parts.query).get("keyword") == [query])
        except ValueError:
            return False

    async def _candidates(self, page, query: str, excluded: frozenset[str], private: dict) -> list[dict]:
        # Initialize the logged-in site before opening its search route. A cold
        # search tab can hang before DOM readiness even when the profile is valid.
        await self._goto(page, HOME)
        await self._guard(page)
        await self._goto(page, "https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": query}))
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
                if len(results) >= self.max_results:
                    return list(results.values())
            idle_rounds = idle_rounds + 1 if len(results) == before else 0
            if idle_rounds >= 2:
                break
            await page.mouse.wheel(0, 950)
            await asyncio.sleep(min(3, self.pacing_seconds))
            cards = await self._evaluate(page, CARDS_JS)
        return list(results.values())

    async def _read(self, page, identity: str, card: dict) -> dict:
        await asyncio.sleep(self.pacing_seconds)
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

    async def _invoke(self, operation: Callable[[], Awaitable], *, query: bool, execute: Execute | None) -> dict:
        if execute is None:
            data = await operation()
            if not query:
                data = FactEvidence.model_validate(data | {"evidence_id": uuid4().hex,
                    "retrieved_at": datetime.now(timezone.utc)}).model_dump()
            return {"data": data}
        result = await execute(operation, query=query)
        if result.get("error"):
            error = str(result["error"])
            if "XiaohongshuLoginRequired" in error:
                raise XiaohongshuLoginRequired("小红书需要重新登录；请单独运行登录命令")
            if "XiaohongshuAccessRestricted" in error:
                raise XiaohongshuAccessRestricted("小红书验证或访问限制阻止了后台采集")
            raise XiaohongshuError("小红书搜索或正文读取失败；已登记的材料保留")
        return result

    async def search(self, query: str, *, execute: Execute | None = None,
                     excluded_ids: frozenset[str] = frozenset(), deadline_at: datetime | None = None) -> list[Evidence]:
        """Search and read fresh bodies, registering candidates and each body separately.

        The timeout covers profile-lock waiting and all reads. On failure a
        controlled exception exposes partial_evidence; registered Fact items
        remain in the caller's ledger. Cancellation always propagates.
        """
        query = query.strip()
        if not query or len(query) > 100:
            raise ValueError("小红书搜索关键词须为 1–100 个字符")
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
                    self._owner_task = asyncio.current_task()
                    pages = []
                    try:
                        context = await self._ensure_context()
                        search_page, detail_page = await context.new_page(), await context.new_page()
                        pages = [search_page, detail_page]
                        private = {}
                        result = await self._invoke(lambda: self._candidates(search_page, query,
                            frozenset(identity.lower() for identity in excluded_ids), private), query=True, execute=execute)
                        if not result.get("stopped"):
                            for candidate in result["data"][:self.max_results]:
                                identity = candidate["note_id"]
                                result = await self._invoke(lambda: self._read(detail_page, identity, private[identity]),
                                                            query=False, execute=execute)
                                if result.get("stopped"):
                                    break
                                if result.get("data", {}).get("duplicate"):
                                    continue
                                evidence.append(FactEvidence.model_validate(result["data"]))
                    except (Exception, asyncio.CancelledError):
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
