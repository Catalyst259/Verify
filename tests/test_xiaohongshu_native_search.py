"""Native visible search events preserve exact query binding and safe action errors."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest

from backend.sources import xiaohongshu as xhs
from test_xiaohongshu_source import FakeLocator, fake_source, fast_polling


QUERY = "杭州西湖 公共卫生间"
PREFIX = "https://www.xiaohongshu.com"
KEYWORD = urlencode({"keyword": QUERY})
ENCODED_KEYWORD = urlencode({"keyword": quote(QUERY, safe="")})


@pytest.mark.parametrize("url,expected", [
    (PREFIX + "/search_result?" + KEYWORD, True),
    (PREFIX + "/search_result_ai?" + ENCODED_KEYWORD, True),
    (PREFIX + "/search_result_ai/?" + ENCODED_KEYWORD, True),
    (PREFIX + "/search_result?" + ENCODED_KEYWORD, False),
    (PREFIX + "/search_result_ai?" + urlencode({"keyword": quote(quote(QUERY, safe=""), safe="")}), False),
    (PREFIX + "/search_result_ai?keyword=WRONG", False),
    (PREFIX + "/search_result_ai?" + ENCODED_KEYWORD + "&" + ENCODED_KEYWORD, False),
    (PREFIX + "/search_result_ai?" + urlencode({"keyword": quote(QUERY.replace(" ", "  "), safe="")}), False),
    ("https://foreign.test/search_result_ai?" + ENCODED_KEYWORD, False),
    ("http://www.xiaohongshu.com/search_result_ai?" + ENCODED_KEYWORD, False),
    (PREFIX + "/ai_search_result?" + ENCODED_KEYWORD, False),
    (PREFIX + "/explore?" + ENCODED_KEYWORD, False),
])
def test_search_identity_accepts_only_confirmed_routes_with_exact_single_keyword(url, expected):
    assert xhs.XiaohongshuSource._is_search(SimpleNamespace(url=url), QUERY) is expected


def test_new_layout_focuses_recreated_editor_and_reads_only_requested_note(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        context = await source._ensure_context()
        context.controls.update({"#search-input": False, "#search-input-in-feeds": True})
        try:
            result = await source.search(QUERY, result_limit=1)
            assert len(result) == 1 and result[0].content.startswith("详情标题\n\n原始正文")
            assert context.ui_actions == [("#search-input-in-feeds", "click"),
                ("textarea:focus", "fill"), ("textarea:focus", "Enter")]
            assert context.visits[1] == PREFIX + "/search_result_ai?" + ENCODED_KEYWORD
            metadata = source._identity_metadata(SimpleNamespace(url=context.visits[1]), QUERY)
            assert metadata["path_kind"] == "search" and metadata["keyword_single_decode_exact"]
            assert not metadata["keyword_exact"] and QUERY not in str(metadata)
        finally:
            await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["before_control", "after_focus", "after_legacy_visibility", "after_editor_check", "after_fill"])
def test_native_input_page_redirect_stops_before_next_query_action(tmp_path, monkeypatch, phase):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        context = await source._ensure_context()
        context.controls.update({"#search-input": False, "#search-input-in-feeds": True})
        page = await context.new_page()
        page.url = xhs.HOME
        original_click, original_fill = FakeLocator.click, FakeLocator.fill
        original_visible, original_evaluate = FakeLocator.is_visible, FakeLocator.evaluate

        async def click(control, **kwargs):
            await original_click(control, **kwargs)
            if phase == "after_focus":
                control.page.url = "https://foreign.test/explore?private=PRIVATE"

        async def fill(control, value, **kwargs):
            await original_fill(control, value, **kwargs)
            if phase == "after_fill":
                control.page.url = "https://foreign.test/explore?private=PRIVATE"

        async def is_visible(control):
            visible = await original_visible(control)
            if phase == "after_legacy_visibility" and control.selector == "#search-input":
                control.page.url = "https://foreign.test/explore?private=PRIVATE"
            return visible

        async def evaluate(control, script):
            allowed = await original_evaluate(control, script)
            if phase == "after_editor_check":
                control.page.url = "https://foreign.test/explore?private=PRIVATE"
            return allowed

        monkeypatch.setattr(FakeLocator, "click", click)
        monkeypatch.setattr(FakeLocator, "fill", fill)
        monkeypatch.setattr(FakeLocator, "is_visible", is_visible)
        monkeypatch.setattr(FakeLocator, "evaluate", evaluate)
        if phase == "after_legacy_visibility":
            context.controls["#search-input"] = True
        if phase == "before_control":
            page.url = "https://foreign.test/explore?private=PRIVATE"
        try:
            with pytest.raises(xhs.XiaohongshuError, match="输入页已切换") as failure:
                await source._enter_search(page, QUERY)
            assert "PRIVATE" not in str(failure.value) and QUERY not in str(failure.value)
            assert all(action != "Enter" for _, action in context.ui_actions)
            if phase != "after_fill":
                assert all(action != "fill" for _, action in context.ui_actions)
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_native_action_exception_does_not_export_input_or_signed_url(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)

        async def fill(*args, **kwargs):
            raise RuntimeError(QUERY + " https://www.xiaohongshu.com/explore/private?xsec_token=PRIVATE")

        monkeypatch.setattr(FakeLocator, "fill", fill)
        try:
            with pytest.raises(xhs.XiaohongshuError, match="可见搜索控件操作失败") as failure:
                await source.search(QUERY)
            assert QUERY not in str(failure.value) and "PRIVATE" not in str(failure.value)
            assert not contexts[0].ui_actions and source._context is None
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_native_fill_is_bounded_by_original_query_deadline(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)

        async def fill(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(FakeLocator, "fill", fill)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=.02)
        try:
            with pytest.raises(xhs.XiaohongshuError, match="超过限定时间"):
                await source.search(QUERY, deadline_at=deadline)
            assert contexts[0].visits == [xhs.HOME] and all(page.closed for page in contexts[0].pages)
            assert source._context is contexts[0] and not source._lock.locked()
        finally:
            await source.aclose()
    asyncio.run(scenario())
