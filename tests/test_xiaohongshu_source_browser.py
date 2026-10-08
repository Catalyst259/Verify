"""System Chrome + intercepted synthetic pages; no real XHS account or public HTTP."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.sources.xiaohongshu import (
    HOME,
    XiaohongshuError,
    XiaohongshuLoginRequired,
    XiaohongshuSource,
    canonical_note_id,
)
from backend.verification.subgraphs.facts.model import FactEvidence, FactPlan
from backend.verification.subgraphs.facts.search import SearchSession
from backend.verification.subgraphs.facts.state import FactClaimState

pytestmark = pytest.mark.skipif(os.getenv("VERIFY_BROWSER_TESTS") != "1",
                              reason="显式启用真实系统浏览器与合成页面验证")
CHROME = os.getenv("VERIFY_TEST_BROWSER_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
HOME_NOTE_ID = f"{99:024x}"


def detail_html(identity, *, empty=False):
    content = "" if empty else f"真实 DOM 原文 {identity}，互动数缺失也能读取。"
    return f"""<!doctype html><html><body>
    <div class="note-container"><div class="author"><span class="username">合成作者</span></div></div>
    <div id="detail-title">合成详情标题</div><div id="detail-desc">{content}</div>
    <div class="note-content"><span class="date">2026-10-05</span></div>
    </body></html>"""


def search_html():
    return "<html><body>" + "".join(
        f'<section class="note-item"><a class="cover" href="/explore/{number:024x}?xsec_token=PRIVATE">封面</a>'
        f'<span class="title">卡片标题 {number}</span><a class="author"><span class="name">卡片作者</span></a></section>'
        for number in range(1, 13)) + (
        '<section class="note-item" style="display:none"><a class="cover" href="/explore/ffffffffffffffffffffffff">隐藏</a></section>'
        '</body></html>')


async def intercepted_source(tmp_path, monkeypatch, *, visits, route_detail=None, max_results=10):
    source = XiaohongshuSource(tmp_path / "dedicated-profile", executable_path=CHROME,
                              max_results=max_results, pacing_seconds=0, timeout_seconds=30)
    original = source._ensure_context
    routed = set()

    async def route_request(route):
        url = urlsplit(route.request.url)
        if url.hostname != "www.xiaohongshu.com":
            await route.abort()
            return
        visits.append(url.path)
        if url.path == urlsplit(HOME).path:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=(
                '<html><body><section class="note-item">'
                f'<a class="cover" href="/explore/{HOME_NOTE_ID}?xsec_token=PRIVATE">主页卡片</a>'
                '<span class="title">主页材料不可当作搜索结果</span></section></body></html>'))
            return
        if url.path == "/search_result":
            query = parse_qs(url.query).get("keyword", [""])[0]
            if query == "登录墙":
                html = '<html><body><div class="login-container">请登录</div></body></html>'
            elif query == "无结果":
                html = '<html><body><div class="empty-container">暂无相关笔记</div></body></html>'
            else:
                html = search_html()
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            return
        identity = canonical_note_id(route.request.url)
        if identity is not None:
            if route_detail is not None and await route_detail(route, identity):
                return
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=detail_html(identity))
            return
        await route.abort()

    async def open_context(**kwargs):
        context = await original(**kwargs)
        if id(context) not in routed:
            await context.route("**/*", route_request)
            routed.add(id(context))
        return context

    monkeypatch.setattr(source, "_ensure_context", open_context)
    return source


def hook(ledger, calls):
    async def execute(operation, *, query=False):
        calls.append(query)
        try:
            data = await operation()
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}
        if query:
            assert all("PRIVATE" not in str(candidate) for candidate in data)
            return {"data": data}
        item = FactEvidence.model_validate(data | {"evidence_id": f"registered-{len(calls)}",
                                                  "retrieved_at": datetime.now(timezone.utc)})
        ledger.append(item)
        return {"data": item.model_dump(mode="json")}
    return execute


def test_live_browser_reads_ten_visible_bodies_with_registered_time_and_no_tokens(tmp_path, monkeypatch):
    if not Path(CHROME).exists():
        pytest.skip("本机没有测试用系统 Chrome")
    async def scenario():
        visits, ledger, calls = [], [], []
        source = await intercepted_source(tmp_path, monkeypatch, visits=visits)
        try:
            results = await source.search("合成公园", execute=hook(ledger, calls),
                                          excluded_ids=frozenset((f"{1:024x}",)))
            assert len(results) == 10 and results == ledger
            assert calls == [True] + [False] * 10
            assert visits[:2] == [urlsplit(HOME).path, "/search_result"]
            assert len(visits) == 12 and all(f"/{1:024x}" not in path for path in visits)
            assert all(canonical_note_id(item.url) != HOME_NOTE_ID for item in results)
            assert all(item.source == "小红书 · 合成作者" and item.source_type == "WEB" for item in results)
            assert all(item.content.startswith("合成详情标题\n\n真实 DOM 原文") for item in results)
            assert all(item.published_at.isoformat() == "2026-10-05" for item in results)
            assert all("PRIVATE" not in item.model_dump_json() and item.retrieved_at.tzinfo is not None for item in results)
            assert source._context is not None and all(page.url == "about:blank" for page in source._context.pages)
            assert await source.search("无结果") == []
            with pytest.raises(XiaohongshuLoginRequired):
                await source.search("登录墙")
            assert source._context is None
        finally:
            await source.aclose()
        assert source._playwright is None and source._context is None
    asyncio.run(scenario())


def test_real_browser_warm_home_and_ten_notes_leave_web_budget(tmp_path, monkeypatch):
    async def scenario():
        visits = []
        source = await intercepted_source(tmp_path, monkeypatch, visits=visits)
        plan = FactPlan(claim_id="c0", fact_type="FACILITY", target="合成公园", time_scope="当前",
                        questions=["停车场是否开放？"], evidence_strategy=[
                            {"priority": 1, "source_type": "WEB", "purpose": "核对开放状态"}])
        session = SearchSession(FactClaimState(plan=plan), datetime.now(timezone.utc) + timedelta(seconds=60))
        try:
            results = await source.search("合成公园", execute=session.execute, deadline_at=session.deadline_at)
            assert len(results) == 10 and results == session.evidence
            assert visits[:2] == [urlsplit(HOME).path, "/search_result"] and len(visits) == 12
            assert all(canonical_note_id(item.url) != HOME_NOTE_ID for item in results)
            assert session.round.queries == 1 and session.round.tool_calls == 11

            async def web_candidates():
                return [{"title": "合成网页", "url": "https://example.test/notice"}]

            async def web_body():
                return {"source": "合成网页", "content": "本地测试网页正文：停车场开放。",
                        "url": "https://example.test/notice", "source_type": "WEB"}

            candidates = await session.execute(web_candidates, query=True)
            web = await session.execute(web_body)
            assert candidates["data"] == [{"title": "合成网页", "url": "https://example.test/notice"}]
            assert web["data"]["content"] == "本地测试网页正文：停车场开放。"
            assert session.round.queries == 2 and session.round.tool_calls == 13
            assert session.round.results_per_query == [10, 1] and session.round.new_evidence_count == 11
            assert len(session.evidence) == 11 and session.evidence[:-1] == results
            assert len({item.evidence_id for item in session.evidence}) == 11
            assert session.evidence[-1].source_type == "WEB" and session.result().error is None
        finally:
            await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 404])
def test_real_browser_rejects_http_failure_bodies_and_preserves_registered_partial(tmp_path, monkeypatch, status):
    async def scenario():
        visits, ledger, calls = [], [], []
        async def fail_second(route, identity):
            if identity == f"{2:024x}":
                await route.fulfill(status=status, content_type="text/html", body=detail_html(identity))
                return True
            return False
        source = await intercepted_source(tmp_path, monkeypatch, visits=visits, route_detail=fail_second)
        try:
            with pytest.raises(XiaohongshuError) as failure:
                await source.search("合成公园", execute=hook(ledger, calls))
            assert len(ledger) == 1 and failure.value.partial_evidence == ledger
            assert "PRIVATE" not in str(failure.value) and source._context is None
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_real_browser_cancellation_closes_context_and_preserves_previous_registration(tmp_path, monkeypatch):
    async def scenario():
        visits, ledger, calls, entered = [], [], [], asyncio.Event()
        async def empty_second(route, identity):
            if identity == f"{2:024x}":
                await route.fulfill(status=200, content_type="text/html", body=detail_html(identity, empty=True))
                entered.set()
                return True
            return False
        source = await intercepted_source(tmp_path, monkeypatch, visits=visits, route_detail=empty_second)
        try:
            task = asyncio.create_task(source.search("合成公园", execute=hook(ledger, calls)))
            await asyncio.wait_for(entered.wait(), timeout=20)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(ledger) == 1 and source._context is None and not source._lock.locked()
        finally:
            await source.aclose()
    asyncio.run(scenario())
