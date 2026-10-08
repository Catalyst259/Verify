"""Cold navigation regression using the existing source doubles, without public HTTP."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

# fast_polling 是 pytest fixture，只在函数签名里被引用；静态分析看不见，需 noqa。
from test_xiaohongshu_source import FakePage, execute_hook, fake_source, fast_polling  # noqa: F401

from backend.sources import xiaohongshu as xhs

HOME_NOTE = "ffffffffffffffffffffffff"


@pytest.fixture
def cold_source(tmp_path, monkeypatch, fast_polling):  # noqa: F811  pytest 按参数名解析 fixture
    source, contexts, drivers = fake_source(tmp_path, monkeypatch)
    original_goto, original_evaluate = FakePage.goto, FakePage.evaluate

    async def goto(page, url, **kwargs):
        path = urlsplit(url).path
        if path.rstrip("/") == "/search_result" and not getattr(page, "bootstrapped", False):
            raise PlaywrightTimeout("cold direct search timed out; xsec_token=PRIVATE")
        if url == xhs.HOME:
            gate = getattr(page.context, "home_gate", None)
            if gate is not None:
                gate[0].set()
                await gate[1].wait()
            page.bootstrapped = True
        result = await original_goto(page, url, **kwargs)
        if url == xhs.HOME:
            return SimpleNamespace(status=getattr(page.context, "home_status", 200))
        return result

    async def evaluate(page, script):
        page.context.evaluations = [*getattr(page.context, "evaluations", []), (page.url, script)]
        if page.url == xhs.HOME:
            if script == xhs.GUARD_JS:
                return getattr(page.context, "home_guard", "")
            if script == xhs.CARDS_JS:
                return [{"url": f"https://www.xiaohongshu.com/explore/{HOME_NOTE}",
                         "title": "主页推荐，不能作为关键词搜索证据", "author": "主页作者"}]
        return await original_evaluate(page, script)

    monkeypatch.setattr(FakePage, "goto", goto)
    monkeypatch.setattr(FakePage, "evaluate", evaluate)
    return source, contexts, drivers


def test_bootstrap_recovers_cold_search_and_reads_ten_bodies_with_one_query(cold_source):
    async def scenario():
        source, _, drivers = cold_source
        context = await source._ensure_context()
        cold_page = await context.new_page()
        with pytest.raises(xhs.XiaohongshuError, match="导航超时") as failure:
            await source._goto(cold_page, "https://www.xiaohongshu.com/search_result?keyword=test")
        assert "PRIVATE" not in str(failure.value)
        await cold_page.close()

        ledger, calls = [], []
        result = await source.search("公园", execute=execute_hook(ledger, calls))
        assert result == ledger and len(result) == 10
        assert calls == [True] + [False] * 10
        assert context.visits[:2] == [xhs.HOME, "https://www.xiaohongshu.com/search_result?keyword=%E5%85%AC%E5%9B%AD"]
        assert len(context.visits) == 12
        assert all(xhs.canonical_note_id(item.url) != HOME_NOTE for item in result)
        assert all("主页推荐" not in item.content and "PRIVATE" not in item.model_dump_json() for item in result)
        assert (xhs.HOME, xhs.GUARD_JS) in context.evaluations
        assert (xhs.HOME, xhs.CARDS_JS) not in context.evaluations
        assert all(page.closed for page in context.pages)
        await source.aclose()
        assert context.closed and drivers[0].stopped
    asyncio.run(scenario())


@pytest.mark.parametrize("guard,exception", [
    ("login", xhs.XiaohongshuLoginRequired),
    ("verify", xhs.XiaohongshuAccessRestricted),
])
def test_home_guard_stops_before_search_and_preserves_empty_ledger(cold_source, guard, exception):
    async def scenario():
        source, _, drivers = cold_source
        context = await source._ensure_context()
        context.home_guard = guard
        ledger, calls = [], []
        with pytest.raises(exception) as failure:
            await source.search("公园", execute=execute_hook(ledger, calls))
        assert failure.value.partial_evidence == ledger == []
        assert calls == [True] and context.visits == [xhs.HOME]
        assert context.closed and drivers[0].stopped and source._context is None
        assert not source._lock.locked()
        await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 404, 429])
def test_home_http_failure_is_not_used_as_search_evidence(cold_source, status):
    async def scenario():
        source, _, drivers = cold_source
        context = await source._ensure_context()
        context.home_status = status
        with pytest.raises(xhs.XiaohongshuError) as failure:
            await source.search("公园")
        assert failure.value.partial_evidence == [] and context.visits == [xhs.HOME]
        assert context.closed and drivers[0].stopped and source._context is None
        await source.aclose()
    asyncio.run(scenario())


def test_cancellation_during_home_bootstrap_propagates_and_releases_profile(cold_source, caplog):
    async def scenario():
        source, _, drivers = cold_source
        context = await source._ensure_context()
        entered, block = asyncio.Event(), asyncio.Event()
        context.home_gate = entered, block
        ledger, calls = [], []
        task = asyncio.create_task(source.search("公园", execute=execute_hook(ledger, calls)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [True] and ledger == []
        assert context.closed and drivers[0].stopped and source._context is None
        assert not source._lock.locked()
        assert not any("xiaohongshu_navigation_failed" in record.getMessage() for record in caplog.records)
        await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("error,category,network_error", [
    (PlaywrightTimeout("signed URL xsec_token=PRIVATE keyword=SECRET"), "timeout", None),
    (RuntimeError("Page.goto: net::ERR_NETWORK_CHANGED at https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff?xsec_token=PRIVATE&keyword=SECRET"),
     "navigation", "net::ERR_NETWORK_CHANGED"),
    (RuntimeError("https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff?xsec_token=PRIVATE&keyword=SECRET#SIGNED"),
     "navigation", None),
    (RuntimeError("Page.goto: navigation failed at https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff?xsec_token=PRIVATE&net::ERR_PRIVATE=SECRET"),
     "navigation", None),
    (RuntimeError("Page.goto: net::ERR_PRIVATE at https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff?xsec_token=PRIVATE&keyword=SECRET"),
     "navigation", None),
])
def test_navigation_diagnostics_keep_only_safe_error_metadata(cold_source, monkeypatch, caplog,
                                                            error, category, network_error):
    async def scenario():
        source, _, _ = cold_source
        page = await (await source._ensure_context()).new_page()

        async def fail_goto(url, **kwargs):
            assert kwargs == {"wait_until": "domcontentloaded", "timeout": 25000}
            raise error

        monkeypatch.setattr(page, "goto", fail_goto)
        with pytest.raises(xhs.XiaohongshuError) as failure:
            await source._goto(page, "https://www.xiaohongshu.com/explore/ffffffffffffffffffffffff?xsec_token=PRIVATE&keyword=SECRET#SIGNED")
        messages = [record.getMessage() for record in caplog.records
                    if record.getMessage().startswith("xiaohongshu_navigation_failed ")]
        assert len(messages) == 1
        diagnostic = json.loads(messages[0].split(" ", 1)[1])
        assert set(diagnostic) == {"host", "page", "error_type", "category", "network_error", "elapsed_ms"}
        assert diagnostic["host"] == "www.xiaohongshu.com" and diagnostic["page"] == "detail"
        assert diagnostic["error_type"] == type(error).__name__
        assert diagnostic["category"] == category and diagnostic["network_error"] == network_error
        assert diagnostic["elapsed_ms"] >= 0
        exposed = messages[0] + str(failure.value)
        assert all(secret not in exposed for secret in ("PRIVATE", "SECRET", "SIGNED", "xsec_token", "keyword", "ffffffffffffffffffffffff"))
        await source.aclose()
    asyncio.run(scenario())
