"""Contended first passes read real bodies, retain safe browser state and keep ledgers."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from backend.verification.subgraphs.facts import search
from backend.verification.subgraphs.facts.model import FactPlan, SearchResult
from backend.verification.subgraphs.facts.state import FactClaimState
from test_source_batch_budget import session, query, web
from test_xiaohongshu_source import fake_source, fast_polling


def test_source_slice_collects_only_unseen_candidate_quota(tmp_path, monkeypatch, web):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        current = session(source.for_run(), limit=1)
        input_url = "https://www.xiaohongshu.com/explore/000000000000000000000001"
        current.input_note_ids = frozenset({"000000000000000000000001"})
        first = await query(current)
        second = await query(current)
        assert current.round.results_per_query == [1, 1, 1, 1]
        assert len(first["crawler_evidence"]) == len(second["crawler_evidence"]) == 1
        assert len(current.evidence) == 2 and current.evidence[0].url != current.evidence[1].url
        assert all(item.url != input_url for item in current.evidence)
        assert all(page.closed for page in contexts[0].pages)
        await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("limit", [False, 0, 11, 1.2, "1"])
def test_source_quota_is_validated_without_starting_browser(tmp_path, monkeypatch, limit):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="单次读取上限"):
            await source.search("公园", result_limit=limit)
        assert contexts == []
        await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("callback", ["timely", "skipped", "late"])
def test_registered_first_pass_returns_actual_ledger_without_extra_model_turn(monkeypatch, callback):
    import browser_use

    current = session(None, limit=1)
    browsers = []

    class Browser:
        def __init__(self, **kwargs):
            self.closed = False
            browsers.append(self)

        async def kill(self):
            self.closed = True

    class Agent:
        def __init__(self, **kwargs):
            self.stop = kwargs["register_should_stop_callback"]

        async def run(self, **kwargs):
            assert not await self.stop()

            async def candidates():
                return [{"url": "https://www.xiaohongshu.com/explore/000000000000000000000001"}]

            await current.execute(candidates, query=True)
            assert not await self.stop() and current.completed_result is None

            async def body():
                return {"source": "小红书", "source_type": "WEB",
                        "url": "https://www.xiaohongshu.com/explore/000000000000000000000001",
                        "content": "本文仅介绍公园，不足以确认停车场开放。"}

            await current.execute(body)
            current.update_round(search_error="Xiaohongshu: 访问限制阻止了来源采集")
            if callback == "timely":
                assert await self.stop()
                current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif callback == "late":
                current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                assert await self.stop() and current.completed_result is not None
            return SimpleNamespace(is_successful=lambda: False, final_result=lambda: None)

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(search, "load_config", lambda: {"model": "gpt-4.1", "api_key": "test-only",
        "base_url": "https://model.test/v1", "timeout_seconds": 10, "max_steps": 2, "browser_executable_path": ""})
    result = SearchResult.model_validate_json(asyncio.run(search.run_search(current, "test", "test")))
    assert result.evidence == current.evidence and len(result.evidence) == 1
    assert result.error == "Xiaohongshu: 访问限制阻止了来源采集"
    assert result.evidence[0].evidence_id
    assert all(browser.closed for browser in browsers)


@pytest.mark.parametrize("place,expected", [("公园", "公园"), ("杭州", "杭州 公园")])
def test_source_first_pass_skips_web_agent_model_and_config(tmp_path, monkeypatch, place, expected):
    import browser_use

    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        input_url = "https://www.xiaohongshu.com/explore/000000000000000000000001"
        current = session(source.for_run(), limit=1, input_urls=(input_url,))
        original_deadline = current.deadline_at

        def unexpected(*args, **kwargs):
            raise AssertionError("实际 Source 正文完成后不应调用备用网页、模型或 Agent")

        for name in ("Browser", "Agent", "ChatOpenAI"):
            monkeypatch.setattr(browser_use, name, unexpected)
        monkeypatch.setattr(search, "load_config", unexpected)
        monkeypatch.setattr(search, "page_data", unexpected)
        task = json.dumps({"context": {"target_place": place}, "claim_state": {"plan": {"target": "公园"}}})
        result = SearchResult.model_validate_json(await search.run_search(current, "test", task))
        assert len(result.evidence) == 1 and result.evidence == current.evidence
        assert result.evidence[0].url != input_url and result.evidence[0].evidence_id
        assert result.error is None and current.deadline_at == original_deadline
        assert current.round.queries == 1 and current.round.tool_calls == 2
        assert current.round.results_per_query == [1] and current.completed_result is not None
        assert contexts[0].ui_actions[0] == ("#search-input", "fill")
        from urllib.parse import parse_qs, urlsplit
        assert parse_qs(urlsplit(contexts[0].visits[1]).query)["keyword"] == [expected]
        assert all(page.closed for page in contexts[0].pages)
        await source.aclose()

    asyncio.run(exercise())


def test_source_first_pass_cache_keeps_each_claim_evidence_ids(tmp_path, monkeypatch):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = source.for_run()
        first, second = session(shared, limit=1, claim_id="c0"), session(shared, limit=1, claim_id="c1")
        task = json.dumps({"context": {"target_place": "公园"}, "claim_state": {"plan": {"target": "公园"}}})
        results = [SearchResult.model_validate_json(await search.run_search(current, "test", task))
                   for current in (first, second)]
        assert [result.claim_id for result in results] == ["c0", "c1"]
        assert results[0].evidence[0].url == results[1].evidence[0].url
        assert results[0].evidence[0].retrieved_at == results[1].evidence[0].retrieved_at
        assert results[0].evidence[0].evidence_id != results[1].evidence[0].evidence_id
        assert all(page.closed for page in contexts[0].pages)
        await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["empty", "candidate_only", "input_body", "wrong_web", "error"])
def test_incomplete_source_first_pass_retains_errors_and_uses_original_agent(monkeypatch, outcome):
    import browser_use
    from backend.sources.xiaohongshu import XiaohongshuAccessRestricted

    input_url = "https://www.xiaohongshu.com/explore/000000000000000000000001"
    sources, browsers = [], []

    class Source:
        async def search(self, query, *, execute, excluded_ids, deadline_at, result_limit):
            sources.append(query)
            assert query == "杭州 公园" and result_limit == 1 and deadline_at == original_deadline
            assert "000000000000000000000001" in excluded_ids
            async def value(data):
                return data
            if outcome == "error":
                raise XiaohongshuAccessRestricted("需要手动验证")
            if outcome == "candidate_only":
                await execute(lambda: value([{"url": input_url}]), query=True)
            if outcome in {"input_body", "wrong_web"}:
                await execute(lambda: value({"source": "已读取网页", "source_type": "WEB", "content": "正文",
                                            "url": input_url if outcome == "input_body" else "https://park.test/notice"}))

    class Browser:
        def __init__(self, **kwargs):
            self.closed = False
            browsers.append(self)
        async def kill(self):
            self.closed = True

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["initial_actions"] is None
            self.tools = kwargs["tools"]
        async def run(self, **kwargs):
            assert current.deadline_at == original_deadline
            done = await self.tools.registry.execute_action("done", {})
            return SimpleNamespace(is_successful=lambda: True, final_result=lambda: done.extracted_content)

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(search, "load_config", lambda: {"model": "gpt-4.1", "api_key": "test-only",
        "base_url": "https://model.test/v1", "timeout_seconds": 10, "max_steps": 2, "browser_executable_path": ""})
    current = session(Source(), limit=1, input_urls=(input_url,))
    original_deadline = current.deadline_at
    task = json.dumps({"context": {"target_place": "杭州"}, "claim_state": {"plan": {"target": "公园"}}})
    result = SearchResult.model_validate_json(asyncio.run(search.run_search(current, "test", task)))
    assert sources == ["杭州 公园"] and browsers and all(browser.closed for browser in browsers)
    assert result.evidence == current.evidence and len(result.evidence) == int(outcome == "wrong_web")
    if outcome == "error":
        assert "XiaohongshuAccessRestricted: 需要手动验证" in result.error
    elif outcome == "input_body":
        assert "输入笔记不能作为" in result.error
    else:
        assert result.error is None


def test_source_first_pass_preserves_post_body_failure_at_deadline(monkeypatch):
    class Source:
        async def search(self, query, *, execute, deadline_at, **kwargs):
            async def body():
                return {"source": "小红书作者", "source_type": "WEB", "content": "实际公开正文",
                        "url": "https://www.xiaohongshu.com/explore/000000000000000000000002"}
            await execute(body)
            assert current.completed_result is not None
            current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            raise TimeoutError("正文后清理到达截止时间")

    def unexpected(*args, **kwargs):
        raise AssertionError("截止前完成的正文不应启动备用 Agent")

    monkeypatch.setattr(search, "load_config", unexpected)
    current = session(Source(), limit=1)
    task = json.dumps({"context": {"target_place": "公园"}, "claim_state": {"plan": {"target": "公园"}}})
    result = SearchResult.model_validate_json(asyncio.run(search.run_search(current, "test", task)))
    assert len(result.evidence) == 1 and result.evidence == current.evidence
    assert "TimeoutError: 正文后清理到达截止时间" in result.error
    assert current.completed_result == result.model_dump_json()


@pytest.mark.parametrize("body_first", [False, True])
def test_cancelled_source_first_pass_propagates_and_preserves_actual_ledger(monkeypatch, body_first):
    async def exercise():
        entered, closed = asyncio.Event(), asyncio.Event()
        class Source:
            async def search(self, query, *, execute, **kwargs):
                try:
                    if body_first:
                        async def body():
                            return {"source": "小红书作者", "source_type": "WEB", "content": "实际公开正文",
                                    "url": "https://www.xiaohongshu.com/explore/000000000000000000000002"}
                        await execute(body)
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        def unexpected(*args, **kwargs):
            raise AssertionError("来源取消后不应启动备用 Agent")
        monkeypatch.setattr(search, "load_config", unexpected)
        current = session(Source(), limit=1)
        original_deadline = current.deadline_at
        task = json.dumps({"context": {"target_place": "公园"}, "claim_state": {"plan": {"target": "公园"}}})
        running = asyncio.create_task(search.run_search(current, "test", task))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert closed.is_set() and len(current.evidence) == int(body_first)
        assert (current.completed_result is not None) == body_first
        assert current.deadline_at == original_deadline
    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["before_body", "during_body"])
def test_late_source_without_timely_snapshot_cannot_start_another_browser(monkeypatch, stage):
    class Source:
        async def search(self, query, *, execute, **kwargs):
            async def body():
                current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                return {"source": "小红书作者", "source_type": "WEB", "content": "到期后返回的正文",
                        "url": "https://www.xiaohongshu.com/explore/000000000000000000000002"}
            if stage == "before_body":
                current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await execute(body)

    def unexpected(*args, **kwargs):
        raise AssertionError("没有剩余预算时不应创建另一个浏览器或模型")
    monkeypatch.setattr(search, "load_config", unexpected)
    current = session(Source(), limit=1)
    task = json.dumps({"context": {"target_place": "公园"}, "claim_state": {"plan": {"target": "公园"}}})
    with pytest.raises(TimeoutError, match="内部截止"):
        asyncio.run(search.run_search(current, "test", task))
    assert current.completed_result is None
    assert len(current.evidence) == int(stage == "during_body")


def test_source_and_web_tools_still_start_in_parallel(monkeypatch):
    async def exercise():
        source_entered, web_entered = asyncio.Event(), asyncio.Event()
        class Source:
            async def search(self, query, *, execute, **kwargs):
                source_entered.set()
                await web_entered.wait()
                async def value(data):
                    return data
                await execute(lambda: value([{"note_id": "000000000000000000000002"}]), query=True)
                await execute(lambda: value({"source": "小红书作者", "source_type": "WEB", "content": "实际正文",
                    "url": "https://www.xiaohongshu.com/explore/000000000000000000000002"}))
        async def page_data(browser, url, script, **kwargs):
            web_entered.set()
            await source_entered.wait()
            return {"results": [{"title": "公告", "url": "https://park.test/notice"}], "empty": False}
        monkeypatch.setattr(search, "page_data", page_data)
        current = session(Source(), limit=1)
        async with asyncio.timeout(2):
            result = await query(current)
        assert source_entered.is_set() and web_entered.is_set()
        assert result["data"][0]["url"] == "https://park.test/notice"
        assert len(result["crawler_evidence"]) == 1 and result["crawler_error"] is None
        assert current.round.queries == 2 and current.round.tool_calls == 3
    asyncio.run(exercise())


def test_prior_round_notes_do_not_fill_the_next_first_pass_quota(tmp_path, monkeypatch, web):
    async def exercise():
        source, _, _ = fake_source(tmp_path, monkeypatch)
        shared = source.for_run()
        first = session(shared, limit=1)
        await query(first)
        prior_claim = first.claim_id
        plan = FactPlan(claim_id=prior_claim, fact_type="FACILITY", target="公园", time_scope="当前",
                        questions=["停车场是否开放？"],
                        evidence_strategy=[{"priority": 1, "source_type": "WEB", "purpose": "核对开放"}])
        follow_up = search.SearchSession(
            FactClaimState(plan=plan, evidence=first.evidence, rounds=[first.round]),
            first.deadline_at, evidence_sources={"xiaohongshu": shared}, source_result_limit=1)
        result = await query(follow_up)
        assert follow_up.claim_id == prior_claim
        assert len(result["crawler_evidence"]) == 1 and follow_up.evidence[0].url != first.evidence[0].url
        await source.aclose()
    asyncio.run(exercise())
