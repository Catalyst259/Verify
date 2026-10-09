"""Fake monotonic time verifies note access spacing without real waits or network."""

import asyncio
from collections import deque
from types import SimpleNamespace

from playwright.async_api import TimeoutError as BrowserTimeout
import pytest

from backend.sources import xiaohongshu as xhs
from test_xiaohongshu_source import fake_source


NOTE_ID = "65abcdef0123456789abcdef"
NOTE = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=PRIVATE"
SHORT = "https://xhslink.com/o/example"
REAL_SLEEP = asyncio.sleep


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0., []
        self.early_wakes = deque()
        self.block = False
        self.entered = asyncio.Event()

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.block:
            self.entered.set()
            await asyncio.Event().wait()
        self.now += min(seconds, self.early_wakes.popleft()) if self.early_wakes else seconds
        await REAL_SLEEP(0)


class Page:
    def __init__(self, clock, *, status=200, error=None, navigation_seconds=.25):
        self.clock, self.status, self.error = clock, status, error
        self.navigation_seconds = navigation_seconds
        self.starts, self.url = [], "about:blank"
        self.entered = asyncio.Event()
        self.block_navigation = False

    async def goto(self, url, **kwargs):
        self.starts.append((self.clock.now, url))
        self.entered.set()
        if self.block_navigation:
            await asyncio.Event().wait()
        self.clock.now += self.navigation_seconds
        self.url = NOTE if url == SHORT else url
        if self.error:
            raise self.error
        return SimpleNamespace(status=self.status)

    async def route(self, *args):
        pass  # Simulated browser resolves the allowed share link in goto above.

    async def evaluate(self, script):
        if script == xhs.GUARD_JS:
            return ""
        if script == xhs.DETAIL_JS:
            return {"title": "公开笔记", "content": "公开正文", "author": "作者", "publish_time": "",
                    "detail_found": True}
        raise AssertionError("Unexpected DOM script")

    async def close(self):
        pass


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(xhs, "perf_counter", value)
    monkeypatch.setattr(xhs.asyncio, "sleep", value.sleep)
    return value


def test_first_interval_counts_initialization_and_home_without_pacing_home(tmp_path, clock):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        page = Page(clock)
        clock.now += 3
        await source._goto(page, xhs.HOME)
        await source._goto(page, NOTE)
        assert page.starts[0][0] == 3 and page.starts[1][0] == 4
        assert clock.sleeps == [.75]
    asyncio.run(scenario())


def test_completed_search_time_consumes_interval_without_another_fixed_sleep(tmp_path, clock, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=1)
        source.pacing_seconds = 4
        starts = []

        async def candidates(page, query, excluded, private, limit=None):
            clock.now += 7
            identity = f"{1:024x}"
            private[identity] = {"url": f"https://www.xiaohongshu.com/explore/{identity}"}
            return [{"note_id": identity}]

        original_goto = source._goto

        async def goto(page, url):
            starts.append(clock.now)
            return await original_goto(page, url)

        monkeypatch.setattr(source, "_candidates", candidates)
        monkeypatch.setattr(source, "_goto", goto)
        try:
            assert len(await source.search("公园", result_limit=1)) == 1
            assert len(await source.search("停车", result_limit=1)) == 1
            assert starts == [7, 14.5]
            assert clock.sleeps == [.5, .5]  # Existing two-snapshot DOM stability remains.
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_fast_successive_notes_and_early_wakeup_still_keep_configured_interval(tmp_path, clock):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        page = Page(clock)
        clock.early_wakes.extend([1, 1])
        await source._goto(page, NOTE)
        first_finished = clock.now
        await source._goto(page, NOTE)
        assert clock.sleeps[:3] == [4, 3, 2]
        assert page.starts[0][0] == 4
        assert page.starts[1][0] >= first_finished + 4
        assert page.starts[1][0] - page.starts[0][0] >= 4
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["http", "network", "timeout"])
def test_failed_navigation_records_interval_and_reset_does_not_clear_it(tmp_path, clock, failure):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        clock.now += 4
        error = RuntimeError("PRIVATE") if failure == "network" else BrowserTimeout("PRIVATE") if failure == "timeout" else None
        page = Page(clock, status=403 if failure == "http" else 200, error=error)
        with pytest.raises(xhs.XiaohongshuError) as rejected:
            await source._goto(page, NOTE)
        assert "PRIVATE" not in str(rejected.value)
        finished = clock.now
        await source._reset_browser()
        source.for_run()
        assert source._last_detail_navigation == finished
        next_page = Page(clock)
        await source._goto(next_page, NOTE)
        assert next_page.starts[0][0] >= finished + 4
    asyncio.run(scenario())


def test_cancellation_after_navigation_starts_preserves_interval(tmp_path, clock):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        clock.now += 4
        page = Page(clock)
        page.block_navigation = True
        task = asyncio.create_task(source._goto(page, NOTE))
        await page.entered.wait()
        clock.now += 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        finished = clock.now
        assert source._last_detail_navigation == finished
        next_page = Page(clock)
        await source._goto(next_page, NOTE)
        assert next_page.starts[0][0] >= finished + 4
    asyncio.run(scenario())


def test_cancellation_while_waiting_does_not_claim_a_navigation(tmp_path, clock):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        page = Page(clock)
        clock.block = True
        task = asyncio.create_task(source._goto(page, NOTE))
        await clock.entered.wait()
        clock.now += 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not page.starts and source._last_detail_navigation == 0
        clock.block = False
        await source._goto(page, NOTE)
        assert page.starts[0][0] == 4 and clock.sleeps == [4, 2]
    asyncio.run(scenario())


def test_link_reader_short_link_and_search_detail_share_owner_interval(tmp_path, clock, monkeypatch):
    async def scenario():
        source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=4)
        pages = []

        class Context:
            async def new_page(self):
                page = Page(clock, navigation_seconds=1)
                pages.append(page)
                return page

        context = Context()

        async def ensure(**kwargs):
            source._context = context
            return context

        async def images(*args):
            return ()

        monkeypatch.setattr(source, "_ensure_context", ensure)
        monkeypatch.setattr(source, "_note_images", images)
        await source._read(Page(clock, navigation_seconds=1), NOTE_ID, {"url": NOTE})
        first_finished = source._last_detail_navigation
        material = await source.for_run().read_note(SHORT)
        assert material.canonical_url == NOTE.split("?")[0]
        short_start = next(stamp for stamp, url in pages[0].starts if url == SHORT)
        assert short_start >= first_finished + 4
        link_finished = source._last_detail_navigation
        next_page = Page(clock)
        await source._read(next_page, NOTE_ID, {"url": NOTE})
        assert next_page.starts[0][0] >= link_finished + 4
    asyncio.run(scenario())


def test_cached_body_does_not_advance_navigation_clock(tmp_path, clock, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=1)
        source.pacing_seconds = 4
        run = source.for_run()
        try:
            first = await run.search("公园", result_limit=1)
            last = source._last_detail_navigation
            visits = len(contexts[0].visits)
            second = await run.search("公园", result_limit=1)
            assert len(contexts[0].visits) == visits + 2  # Home and native query, no second detail.
            assert source._last_detail_navigation == last
            assert first[0].retrieved_at == second[0].retrieved_at
            assert first[0].evidence_id != second[0].evidence_id
        finally:
            await source.aclose()
    asyncio.run(scenario())
