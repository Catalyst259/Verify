"""Source slices use real adapter contracts with local browser and Agent doubles."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from backend.verification.subgraphs.facts import search
from backend.verification.subgraphs.facts.model import FactPlan, SearchResult
from backend.verification.subgraphs.facts.state import FactClaimState
from test_xiaohongshu_source import fake_source


def session(source, *, limit=None, seconds=30, claim_id="c0", input_urls=()):
    plan = FactPlan(claim_id=claim_id, fact_type="FACILITY", target="公园", time_scope="当前",
                    questions=["停车场是否开放？"],
                    evidence_strategy=[{"priority": 1, "source_type": "WEB", "purpose": "核对开放"}])
    return search.SearchSession(FactClaimState(plan=plan),
        datetime.now(timezone.utc) + timedelta(seconds=seconds),
        evidence_sources={"xiaohongshu": source}, source_result_limit=limit, input_urls=input_urls)


@pytest.fixture
def web(monkeypatch):
    async def page_data(browser, url, script, *, diagnostic):
        return {"results": [{"title": "公开公告", "url": "https://park.test/notice"}], "empty": False}
    monkeypatch.setattr(search, "page_data", page_data)


async def query(current):
    tools = search.create_tools(current, object())
    output = await tools.registry.execute_action("search_web", {"query": "公园停车场开放"})
    return json.loads(output.extracted_content)


@pytest.mark.parametrize("limit,count", [(None, 10), (2, 2)])
def test_source_limit_bounds_new_bodies_and_preserves_idle_default(tmp_path, monkeypatch, web, limit, count):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch)
        current = session(source, limit=limit)
        result = await query(current)
        assert len(result["crawler_evidence"]) == count
        assert len(current.evidence) == current.round.new_evidence_count == count
        assert current.round.tool_calls == count + 2 and current.round.queries == 2
        assert result["crawler_error"] is None and current.result().error is None
        assert current.remaining_budget()["max_source_results_per_query"] == count
        assert len(contexts[0].visits) == count + 2
        assert all(page.closed for page in contexts[0].pages)
        await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("seconds", [.4, 8, 40])
def test_source_gets_positive_fractional_budget_even_below_ten_seconds(monkeypatch, web, seconds):
    class Source:
        async def search(self, query, *, deadline_at, **kwargs):
            now = datetime.now(timezone.utc)
            source_window = (deadline_at - now).total_seconds()
            session_window = (current.deadline_at - now).total_seconds()
            assert 0 < source_window < session_window
            assert source_window == pytest.approx(session_window - min(10, session_window * .25), abs=.02)
            self.deadline_at = deadline_at

    async def exercise():
        nonlocal current
        source = Source()
        current = session(source, seconds=seconds, limit=2)
        original_deadline = current.deadline_at
        await query(current)
        assert current.deadline_at == original_deadline
        assert source.deadline_at < current.deadline_at
    current = None
    asyncio.run(exercise())


def test_run_cache_keeps_per_claim_ids_and_input_exclusion_under_slice_limit(tmp_path, monkeypatch, web):
    async def exercise():
        source, _, _ = fake_source(tmp_path, monkeypatch)
        shared = source.for_run()
        input_url = "https://www.xiaohongshu.com/explore/000000000000000000000001"
        first = session(shared, limit=2, claim_id="c0", input_urls=(input_url,))
        second = session(shared, limit=2, claim_id="c1", input_urls=(input_url,))
        await query(first)
        await query(second)
        assert len(first.evidence) == len(second.evidence) == 2
        assert [item.url for item in first.evidence] == [item.url for item in second.evidence]
        assert all(item.url != input_url for item in [*first.evidence, *second.evidence])
        assert len({item.evidence_id for item in [*first.evidence, *second.evidence]}) == 4
        assert [item.retrieved_at for item in first.evidence] == [item.retrieved_at for item in second.evidence]
        assert first.result().claim_id == "c0" and second.result().claim_id == "c1"
        await source.aclose()
    asyncio.run(exercise())


def test_duplicate_bodies_do_not_consume_the_next_query_new_evidence_limit(tmp_path, monkeypatch, web):
    async def exercise():
        source, _, _ = fake_source(tmp_path, monkeypatch)
        current = session(source.for_run(), limit=2)
        assert len((await query(current))["crawler_evidence"]) == 2
        second = await query(current)
        assert len(second["crawler_evidence"]) == 2 and len(current.evidence) == 4
        assert len({item.url for item in current.evidence}) == 4
        assert current.result().error is None
        await source.aclose()
    asyncio.run(exercise())


def test_source_timeout_keeps_material_and_real_error_before_done(monkeypatch, web):
    class Source:
        async def search(self, query, *, execute, deadline_at, **kwargs):
            async def value(data):
                return data
            async with asyncio.timeout((deadline_at - datetime.now(timezone.utc)).total_seconds()):
                await execute(lambda: value([{"note_id": "000000000000000000000002"}]), query=True)
                await execute(lambda: value({"source": "小红书公开作者", "source_type": "WEB",
                    "content": "停车场对游客开放。", "url": "https://www.xiaohongshu.com/explore/000000000000000000000002"}))
                await asyncio.sleep(30)

    async def exercise():
        current = session(Source(), limit=2, seconds=.4)
        result = await query(current)
        assert not result["deadline_reached"] and len(result["crawler_evidence"]) == 1
        assert "TimeoutError" in result["crawler_error"]
        done = await search.create_tools(current, object()).registry.execute_action("done", {})
        final = SearchResult.model_validate_json(done.extracted_content)
        assert len(final.evidence) == 1 and "TimeoutError" in final.error
        assert current.completed_result == done.extracted_content
    asyncio.run(exercise())


@pytest.mark.parametrize("finish_before_deadline", [True, False])
def test_done_is_captured_before_agent_bookkeeping_but_late_completion_is_timeout(monkeypatch, finish_before_deadline):
    import browser_use

    browsers = []

    class Browser:
        def __init__(self, **kwargs):
            self.closed = False
            browsers.append(self)

        async def kill(self):
            self.closed = True

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            assert "max_source_results_per_query" in kwargs["extend_system_message"]

        async def run(self, **kwargs):
            if not finish_before_deadline:
                current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            done = await self.tools.registry.execute_action("done", {})
            current.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            return SimpleNamespace(is_successful=lambda: True, final_result=lambda: done.extracted_content)

    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(search, "load_config", lambda: {"model": "gpt-4.1", "api_key": "test-only",
        "base_url": "https://model.test/v1", "timeout_seconds": 10, "max_steps": 2, "browser_executable_path": ""})
    current = session(None, limit=2)
    if finish_before_deadline:
        output = asyncio.run(search.run_search(current, "Fact Search", "test-only task"))
        assert output == current.completed_result and SearchResult.model_validate_json(output).error is None
    else:
        with pytest.raises(TimeoutError, match="内部截止"):
            asyncio.run(search.run_search(current, "Fact Search", "test-only task"))
        assert current.completed_result is None
    assert browsers and all(browser.closed for browser in browsers)
