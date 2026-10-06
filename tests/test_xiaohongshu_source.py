"""Async source contracts use browser doubles and never touch public sites."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.sources import xiaohongshu as xhs
from backend.verification.subgraphs.facts.model import FactEvidence


ID = "65abcdef0123456789abcdef"


def cards(count=10):
    return [{"url": f"https://www.xiaohongshu.com/explore/{number:024x}?xsec_token=PRIVATE",
             "title": f"卡片标题 {number}", "author": "卡片作者"} for number in range(1, count + 1)]


class FakePage:
    def __init__(self, context):
        self.context, self.url, self.closed = context, "about:blank", False
        self.mouse = SimpleNamespace(wheel=self.wheel)

    async def wheel(self, *args):
        pass

    async def goto(self, url, **kwargs):
        identity = xhs.canonical_note_id(url)
        self.context.visits.append(url)
        if identity in self.context.fail_navigation:
            raise RuntimeError(f"failed https://www.xiaohongshu.com/explore/{identity}?xsec_token=PRIVATE")
        self.url = self.context.redirects.get(identity, url)
        return SimpleNamespace(status=self.context.statuses.get(identity, 200))

    async def evaluate(self, script):
        identity = xhs.canonical_note_id(self.url)
        if script == xhs.GUARD_JS:
            return self.context.guards.get(identity, "")
        if script == xhs.CARDS_JS:
            return self.context.cards
        if script == xhs.EMPTY_JS:
            return not self.context.cards
        if script == xhs.DETAIL_JS:
            return {"title": "详情标题", "content": f"原始正文 {identity}", "author": "公开作者",
                    "publish_time": self.context.publish_time, "detail_found": True}
        raise AssertionError("Unexpected DOM script")

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, count=10):
        self.cards = cards(count)
        self.guards, self.redirects, self.statuses = {}, {}, {}
        self.fail_navigation = set()
        self.publish_time = "昨天"
        self.visits, self.pages = [], []
        self.closed = False

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeDriver:
    stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr(xhs.asyncio, "sleep", sleep)
    return original_sleep


def fake_source(tmp_path, monkeypatch, *, count=10, **options):
    source = xhs.XiaohongshuSource(tmp_path / "profile", pacing_seconds=0, **options)
    contexts, drivers = [], []

    async def open_context(**kwargs):
        if source._context is None:
            source._context, source._playwright = FakeContext(count), FakeDriver()
            contexts.append(source._context)
            drivers.append(source._playwright)
        return source._context

    monkeypatch.setattr(source, "_ensure_context", open_context)
    return source, contexts, drivers


def execute_hook(ledger, calls, *, stop_after=None, duplicate_at=None):
    async def execute(operation, *, query=False):
        if stop_after is not None and len(calls) >= stop_after:
            return {"stopped": True}
        calls.append(query)
        try:
            data = await operation()
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}
        if query:
            return {"data": data}
        if duplicate_at == len(calls):
            return {"data": {"duplicate": True}}
        item = FactEvidence.model_validate(data | {"evidence_id": f"code-{len(calls)}",
                                                  "retrieved_at": datetime.now(timezone.utc)})
        ledger.append(item)
        return {"data": item.model_dump(mode="json")}
    return execute


@pytest.mark.parametrize("url", [f"https://WWW.XIAOHONGSHU.COM:443/explore/{ID.upper()}?xsec_token=PRIVATE",
    f"http://xiaohongshu.com:80/search_result/{ID}/#body", f"https://xiaohongshu.com/explore/{ID}",
    f"https://www.xiaohongshu.com:443/discovery/item/{ID}"])
def test_equivalent_note_links_share_an_id(url):
    assert xhs.canonical_note_id(url) == ID


@pytest.mark.parametrize("url", [f"https://user@www.xiaohongshu.com/explore/{ID}",
    f"https://www.xiaohongshu.com.evil.invalid/explore/{ID}",
    f"https://www.xiaohongshu.com:80/explore/{ID}", f"http://xiaohongshu.com:443/explore/{ID}",
    f"https://xiaohongshu.com:invalid/explore/{ID}", f"https://xiaohongshu.com/explore/{ID}/extra",
    f"https://xiaohongshu.com\n.evil.invalid/explore/{ID}", "https://[invalid", None])
def test_untrusted_note_links_do_not_identify_a_source(url):
    assert xhs.canonical_note_id(url) is None


@pytest.mark.parametrize("maximum", [0, 11, True, "10"])
def test_configuration_requires_bounded_integer_result_count(tmp_path, maximum):
    with pytest.raises(ValueError):
        xhs.XiaohongshuSource(tmp_path / "profile", max_results=maximum)


def test_from_config_resolves_default_and_explicit_relative_profile(tmp_path):
    source = xhs.XiaohongshuSource.from_config({}, root=tmp_path, data_directory=tmp_path / "data")
    assert source.profile_path == tmp_path / "data/xiaohongshu-profile" and source.max_results == 10
    source = xhs.XiaohongshuSource.from_config({"xiaohongshu": {"profile_path": "private/profile", "max_results": 3}},
                                            root=tmp_path, data_directory=tmp_path / "data")
    assert source.profile_path == tmp_path / "private/profile" and source.max_results == 3


def test_standalone_reads_ten_real_bodies_without_interaction_counts(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, drivers = fake_source(tmp_path, monkeypatch)
        result = await source.search("公园 咖啡")
        assert len(result) == 10 and all(isinstance(item, FactEvidence) for item in result)
        assert all(item.content.startswith("详情标题\n\n原始正文") and item.source_type == "WEB" for item in result)
        assert all(item.source == "小红书 · 公开作者" and item.published_at is None for item in result)
        assert len({item.evidence_id for item in result}) == 10
        assert all("PRIVATE" not in item.model_dump_json() for item in result)
        assert contexts[0].visits[:2] == [xhs.HOME, "https://www.xiaohongshu.com/search_result?keyword=%E5%85%AC%E5%9B%AD+%E5%92%96%E5%95%A1"]
        assert len(contexts[0].visits) == 12 and all(page.closed for page in contexts[0].pages)
        await source.aclose()
        assert contexts[0].closed and drivers[0].stopped
    asyncio.run(scenario())


def test_hook_counts_candidates_and_each_read_and_obeys_exclusion_stopped_duplicate(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        ledger, calls = [], []
        result = await source.search("公园", excluded_ids=frozenset((f"{1:024x}",)),
                                     execute=execute_hook(ledger, calls, stop_after=4, duplicate_at=3))
        assert calls == [True, False, False, False]
        assert result == ledger and len(result) == 2
        assert all(xhs.canonical_note_id(item.url) != f"{1:024x}" for item in result)
        assert all(item.evidence_id.startswith("code-") for item in result)
        assert all(f"/{1:024x}?" not in url for url in contexts[0].visits)
        await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["login", "verify", "status", "redirect", "navigation"])
def test_later_failure_preserves_registered_partial_and_redacts_browser_errors(tmp_path, monkeypatch, failure):
    async def scenario():
        source, contexts, drivers = fake_source(tmp_path, monkeypatch)
        context = await source._ensure_context()
        identity = f"{2:024x}"
        if failure in ("login", "verify"):
            context.guards[identity] = failure
        elif failure == "status":
            context.statuses[identity] = 429
        elif failure == "redirect":
            context.redirects[identity] = f"https://xiaohongshu.com/explore/{3:024x}?xsec_token=PRIVATE"
        else:
            context.fail_navigation.add(identity)
        ledger, calls = [], []
        with pytest.raises(xhs.XiaohongshuError) as failure_info:
            await source.search("公园", execute=execute_hook(ledger, calls))
        assert len(ledger) == 1 and failure_info.value.partial_evidence == ledger
        assert "PRIVATE" not in str(failure_info.value) and "xsec_token" not in str(failure_info.value)
        assert contexts[0].closed and drivers[0].stopped
        await source.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("publication,expected_type", [("2026-10-05", date),
    ("2026-10-05T00:00:00+08:00", datetime), ("昨天 上海", type(None))])
def test_publication_preserves_precision(tmp_path, monkeypatch, publication, expected_type):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        context = await source._ensure_context()
        context.publish_time = publication
        assert type((await source.search("公园"))[0].published_at) is expected_type
        await source.aclose()
    asyncio.run(scenario())


def test_cancellation_resets_context_before_next_query_uses_profile(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, drivers = fake_source(tmp_path, monkeypatch, max_results=1)
        entered, block = asyncio.Event(), asyncio.Event()
        original_read = source._read

        async def blocked_read(*args):
            entered.set()
            await block.wait()
        monkeypatch.setattr(source, "_read", blocked_read)
        task = asyncio.create_task(source.search("公园"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert contexts[0].closed and drivers[0].stopped and not source._lock.locked()
        monkeypatch.setattr(source, "_read", original_read)
        assert len(await source.search("第二关键词")) == 1 and len(contexts) == 2
        await source.aclose()
    asyncio.run(scenario())


def test_cancellation_during_page_cleanup_resets_browser(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, drivers = fake_source(tmp_path, monkeypatch, max_results=1)
        entered, block, ledger, calls = asyncio.Event(), asyncio.Event(), [], []
        async def blocked_close(self):
            entered.set()
            await block.wait()
        monkeypatch.setattr(FakePage, "close", blocked_close)
        task = asyncio.create_task(source.search("公园", execute=execute_hook(ledger, calls)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(ledger) == 1 and contexts[0].closed and drivers[0].stopped
        assert not source._lock.locked() and source._context is None
        await source.aclose()
    asyncio.run(scenario())


def test_second_page_creation_failure_closes_first_page_context_and_driver(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, drivers = fake_source(tmp_path, monkeypatch)
        context = await source._ensure_context()
        original = context.new_page
        async def fail_second_page():
            if context.pages:
                raise xhs.XiaohongshuError("详情页面创建失败")
            return await original()
        monkeypatch.setattr(context, "new_page", fail_second_page)
        with pytest.raises(xhs.XiaohongshuError):
            await source.search("公园")
        assert contexts[0].closed and drivers[0].stopped and source._context is None
        assert not source._lock.locked()
        await source.aclose()
    asyncio.run(scenario())


def test_timeout_includes_lock_wait_and_shutdown_cancels_active_query(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, timeout_seconds=0.03)
        await source._lock.acquire()
        try:
            with pytest.raises(xhs.XiaohongshuError, match="等待浏览器资料锁"):
                await source.search("公园")
            assert not contexts
        finally:
            source._lock.release()
        source.timeout_seconds = 120
        entered, block = asyncio.Event(), asyncio.Event()
        async def blocked_candidates(*args):
            entered.set()
            await block.wait()
        monkeypatch.setattr(source, "_candidates", blocked_candidates)
        task = asyncio.create_task(source.search("公园"))
        await entered.wait()
        await source.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert contexts[0].closed and source._closed
    asyncio.run(scenario())


def test_profile_access_is_serialized_and_deadline_is_aware(tmp_path, monkeypatch, fast_polling):
    async def scenario():
        source, _, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        active, maximum = 0, 0
        original = source._candidates
        async def guarded(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await fast_polling(0.02)
            try:
                return await original(*args)
            finally:
                active -= 1
        monkeypatch.setattr(source, "_candidates", guarded)
        results = await asyncio.gather(source.search("公园"), source.search("咖啡"))
        assert maximum == 1 and all(len(result) == 1 for result in results)
        with pytest.raises(ValueError, match="带时区"):
            await source.search("公园", deadline_at=datetime.now())
        with pytest.raises(xhs.XiaohongshuError, match="限定时间"):
            await source.search("公园", deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        await source.aclose()
    asyncio.run(scenario())


def test_login_cli_does_not_require_model_key(tmp_path, monkeypatch, capsys):
    from backend.extraction import agent

    called = []
    monkeypatch.setattr(agent, "read_config", lambda: {"api_key": ""})
    async def login(self):
        called.append(self.profile_path)
    monkeypatch.setattr(xhs.XiaohongshuSource, "login", login)
    assert xhs.main(["--login"]) == 0 and len(called) == 1
    assert "登录有效性由下一次后台查询检测" in capsys.readouterr().out
