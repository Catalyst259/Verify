"""SPA readiness waits retain strict route/keyword guards and export no signed links."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlsplit

import pytest

from backend.sources import xiaohongshu as xhs
from test_xiaohongshu_source import FakePage, fake_source, fast_polling


def test_home_control_and_transient_search_route_settle_before_cards(tmp_path, monkeypatch):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        original_goto, original_evaluate = FakePage.goto, FakePage.evaluate
        home_polls, search_polls, card_reads = 0, 0, 0

        async def goto(page, url, **kwargs):
            await original_goto(page, url, **kwargs)
            if urlsplit(url).path == "/search_result":
                page.pending_search = url
                page.url = xhs.HOME

        async def evaluate(page, script):
            nonlocal home_polls, search_polls, card_reads
            if script == xhs.HOME_READY_JS:
                home_polls += 1
                return home_polls >= 3
            if script == xhs.GUARD_JS and hasattr(page, "pending_search"):
                search_polls += 1
                if search_polls >= 3:
                    page.url = page.pending_search
            if script == xhs.CARDS_JS:
                card_reads += 1
                assert source._is_search(page, "公园")
            return await original_evaluate(page, script)

        monkeypatch.setattr(FakePage, "goto", goto)
        monkeypatch.setattr(FakePage, "evaluate", evaluate)
        try:
            assert len(await source.search("公园", result_limit=1)) == 1
            assert home_polls == 3 and search_polls >= 3 and card_reads == 1
        finally:
            await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("destination,category", [
    (xhs.HOME, "home"),
    ("https://www.xiaohongshu.com/search_result?keyword=WRONG&xsec_token=PRIVATE", "search"),
    ("https://foreign.test/search_result?keyword=PRIVATE", "search"),
])
def test_permanent_search_redirect_is_rejected_without_reading_cards_or_exporting_terms(
        tmp_path, monkeypatch, caplog, destination, category):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        original_goto, original_evaluate = FakePage.goto, FakePage.evaluate

        async def goto(page, url, **kwargs):
            await original_goto(page, url, **kwargs)
            if urlsplit(url).path == "/search_result":
                page.url = destination

        async def evaluate(page, script):
            assert script != xhs.CARDS_JS
            return await original_evaluate(page, script)

        monkeypatch.setattr(FakePage, "goto", goto)
        monkeypatch.setattr(FakePage, "evaluate", evaluate)
        try:
            with pytest.raises(xhs.XiaohongshuError, match="等待页面就绪"):
                await source.search("公园", result_limit=1)
            assert source._context is None and contexts[0].closed
        finally:
            await source.aclose()
    asyncio.run(exercise())
    diagnostics = [row.getMessage() for row in caplog.records if row.getMessage().startswith("xiaohongshu_identity_failed ")]
    assert len(diagnostics) == 1
    metadata = json.loads(diagnostics[0].split(" ", 1)[1])
    assert metadata["phase"] == "search_ready" and metadata["path_kind"] == category
    assert not metadata["keyword_exact"]
    assert "PRIVATE" not in caplog.text and "WRONG" not in caplog.text and "foreign.test" not in caplog.text


def test_home_missing_control_stops_before_opening_search(tmp_path, monkeypatch):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        original_evaluate = FakePage.evaluate

        async def evaluate(page, script):
            if script == xhs.HOME_READY_JS:
                return False
            return await original_evaluate(page, script)

        monkeypatch.setattr(FakePage, "evaluate", evaluate)
        try:
            with pytest.raises(xhs.XiaohongshuError, match="首页搜索控件未就绪"):
                await source.search("公园")
            assert contexts[0].visits == [xhs.HOME]
        finally:
            await source.aclose()
    asyncio.run(exercise())


def test_home_readiness_is_bounded_by_original_query_deadline(tmp_path, monkeypatch):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        original_evaluate = FakePage.evaluate

        async def evaluate(page, script):
            if script == xhs.HOME_READY_JS:
                await asyncio.Event().wait()
            return await original_evaluate(page, script)

        monkeypatch.setattr(FakePage, "evaluate", evaluate)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=.02)
        try:
            with pytest.raises(xhs.XiaohongshuError, match="超过限定时间"):
                await source.search("公园", deadline_at=deadline)
            assert contexts[0].visits == [xhs.HOME] and all(page.closed for page in contexts[0].pages)
            assert not source._lock.locked()
        finally:
            await source.aclose()
    asyncio.run(exercise())
