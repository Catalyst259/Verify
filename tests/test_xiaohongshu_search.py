"""Native Fact source integration, with no external site or model calls."""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.extraction.models import ClaimExtractionResult
from backend.extraction.materials import LinkMaterial
from backend.storage.repository import StorageRepository
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationInput
from backend.verification.service import VerificationService
from backend.verification.subgraphs.facts import search as search_module
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.facts.model import FactEvidence, FactPlan, SearchResult
from backend.verification.subgraphs.facts.search import SearchSession, create_tools
from backend.verification.subgraphs.facts.state import FactClaimState, FactRoundState


def note_id(number):
    return f"{number:024x}"


def note(number):
    return {"source": "小红书作者", "source_type": "WEB", "content": f"笔记正文 {number}",
            "url": f"https://www.xiaohongshu.com/explore/{note_id(number)}"}


def plan():
    return FactPlan(claim_id="c0", fact_type="FACILITY", target="公园", time_scope="当前",
                    questions=["停车场开放吗？"],
                    evidence_strategy=[{"priority": 1, "source_type": "WEB", "purpose": "确认开放"}])


def session(**kwargs):
    return SearchSession(FactClaimState(plan=plan()), datetime.now(timezone.utc) + timedelta(seconds=10), **kwargs)


async def value(data):
    return data


class FakeSource:
    async def read_note(self, url, **kwargs):
        return LinkMaterial(url, url, '公园', '停车场开放。')

    def __init__(self, *, failure=None, distinct_batches=False):
        self.failure = failure
        self.distinct_batches = distinct_batches
        self.calls = []

    async def search(self, query, *, execute, excluded_ids, deadline_at):
        self.calls.append((query, excluded_ids, deadline_at))
        first = 1 + (len(self.calls) - 1) * 10 if self.distinct_batches else 1
        rows = [note(number) for number in range(first, first + 10) if note_id(number) not in excluded_ids]
        result = await execute(lambda: value(rows), query=True)
        evidence = []
        for row in result.get("data", []):
            result = await execute(lambda row=row: value(row))
            if result.get("stopped"):
                break
            if self.failure:
                raise self.failure
            if result.get("data", {}).get("evidence_id"):
                evidence.append(FactEvidence.model_validate(result["data"]))
        return evidence


def mock_web(monkeypatch, *, started=None, wait=None, final_url=None):
    async def page_data(browser, url, script, *, diagnostic):
        if "duckduckgo" in url:
            if started is not None:
                started.set()
            if wait is not None:
                await wait.wait()
            return {"results": [{"title": f"公告 {i}", "url": f"https://park.test/{i}"} for i in range(10)],
                    "empty": False}
        return {"source": "公园公告", "content": "停车场开放。", "url": final_url or url, "published_at": None}
    monkeypatch.setattr(search_module, "page_data", page_data)


async def action(tools, name, **params):
    result = await tools.registry.execute_action(name, params)
    return json.loads(result.extracted_content)


def test_search_automatically_reads_ten_notes_and_keeps_web_body_budget(monkeypatch):
    mock_web(monkeypatch)

    async def exercise():
        source = FakeSource()
        current = session(evidence_sources={"xiaohongshu": source})
        tools = create_tools(current, object())
        assert set(tools.registry.registry.actions) == {"search_web", "read_page", "done"}
        result = await action(tools, "search_web", query="公园停车场")
        assert len(result["data"]) == 10 and len(result["crawler_evidence"]) == 10
        assert result["crawler_error"] is None
        assert all(row["source_type"] == "WEB" and row["evidence_id"] for row in result["crawler_evidence"])
        for number in range(5):
            await action(tools, "read_page", url=f"https://park.test/{number}")
        assert current.round.tool_calls == 17 and current.round.queries == 2
        assert current.round.results_per_query == [10, 10]
        assert len(current.evidence) == current.round.new_evidence_count == 15
        assert current.result().error is None
        assert len(SearchResult.model_validate_json(current.result().model_dump_json()).evidence) == 15
        assert (await current.execute(lambda: value(note(11))))["stopped"]
    asyncio.run(exercise())


def test_parallel_queries_update_their_own_candidate_slots(monkeypatch):
    async def exercise():
        web_started, source_finished = asyncio.Event(), asyncio.Event()
        mock_web(monkeypatch, started=web_started, wait=source_finished)

        class Source:
            async def search(self, query, *, execute, **kwargs):
                async def candidates():
                    await web_started.wait()
                    return list(range(10))
                result = await execute(candidates, query=True)
                assert len(result["data"]) == 10
                source_finished.set()
                return []

        current = session(evidence_sources={"xiaohongshu": Source()})
        await action(create_tools(current, object()), "search_web", query="停车场")
        assert current.round.results_per_query == [10, 10]
        assert current.round.queries == current.round.tool_calls == 2
    asyncio.run(exercise())


def test_second_crawler_batch_preserves_one_web_body(monkeypatch):
    mock_web(monkeypatch)

    async def exercise():
        current = session(evidence_sources={"xiaohongshu": FakeSource(distinct_batches=True)})
        tools = create_tools(current, object())
        first = await action(tools, "search_web", query="停车场开放")
        second = await action(tools, "search_web", query="停车場关闭")
        assert len(first["crawler_evidence"]) == 10 and len(second["crawler_evidence"]) == 4
        assert current.round.tool_calls == 18 and len(current.evidence) == 14
        result = await action(tools, "read_page", url="https://park.test/official", source_type="OFFICIAL")
        assert result["data"]["source_type"] == "OFFICIAL"
        assert current.round.tool_calls == 19 and len(current.evidence) == 15
        assert current.round.results_per_query == [10, 10, 10, 10]
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [RuntimeError("采集失败"), TimeoutError("来源超时")])
def test_crawler_failure_keeps_partial_evidence_and_web_results(monkeypatch, failure):
    mock_web(monkeypatch)

    async def exercise():
        current = session(evidence_sources={"xiaohongshu": FakeSource(failure=failure)})
        tools = create_tools(current, object())
        result = await action(tools, "search_web", query="停车场")
        assert len(result["data"]) == 10 and len(result["crawler_evidence"]) == 1
        assert type(failure).__name__ in result["crawler_error"]
        await action(tools, "read_page", url="https://park.test/notice", source_type="OFFICIAL")
        assert len(current.result().evidence) == 2
        assert current.evidence[-1].source_type == "OFFICIAL"
        assert current.round.tool_calls == 4 and current.round.search_error
    asyncio.run(exercise())


def test_absent_crawler_preserves_web_action_response(monkeypatch):
    mock_web(monkeypatch)

    async def exercise():
        current = session()
        result = await action(create_tools(current, object()), "search_web", query="停车场")
        assert len(result["data"]) == 10 and "crawler_evidence" not in result
        assert current.round.tool_calls == current.round.queries == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("input_url", [
    "http://xiaohongshu.com:80/explore/000000000000000000000001?xsec_token=secret",
    "https://www.xiaohongshu.com:443/discovery/item/000000000000000000000001",
])
def test_input_note_is_excluded_from_source_and_final_redirect(monkeypatch, input_url):
    mock_web(monkeypatch, final_url=note(1)["url"])

    async def exercise():
        source = FakeSource()
        current = session(input_urls=(input_url,), evidence_sources={"xiaohongshu": source})
        tools = create_tools(current, object())
        result = await action(tools, "search_web", query="停车场")
        assert source.calls[0][1] == frozenset({note_id(1)})
        assert len(result["crawler_evidence"]) == 9
        result = await action(tools, "read_page", url="https://park.test/redirect")
        assert "外部独立证据" in result["error"]
        assert len(current.evidence) == 9 and all(row.url != note(1)["url"] for row in current.evidence)
    asyncio.run(exercise())


@pytest.mark.parametrize("scoped_source", [False, True])
def test_each_request_has_its_own_input_note_exclusions_even_for_text_claims(tmp_path, scoped_source):
    received = []
    forks = []

    class ScopedSource(FakeSource):
        def for_run(self):
            source = FakeSource()
            forks.append(source)
            return source

    shared = VerificationCapabilities(evidence_sources={"xiaohongshu": ScopedSource() if scoped_source else FakeSource()})

    async def extract(target_place, *args, **kwargs):
        return ClaimExtractionResult(target_place=target_place, claims=[{
            "claim_id": "c0", "type": "FACT", "content": "停车场开放。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "停车场开放。"}],
        }])

    async def model(prompt, task):
        data = json.loads(task)
        claim_id = data["active_claim_ids"][0]
        if prompt.startswith("# Fact Plan"):
            return json.dumps([plan().model_dump(mode="json") | {"claim_id": claim_id}])
        row = data["claim_states"][claim_id]
        return json.dumps([{"claim_id": claim_id, "assessment": {
            "target": "公园", "time_scope": "当前", "verdict": "SUPPORTED", "confidence": 0.7,
            "evidence_sufficient": True, "reason": "正文可见。",
            "supporting_evidence": [row["evidence"][0]["evidence_id"]],
            "dimensions": dict.fromkeys(["authority", "directness", "recency", "context_match", "independence"]),
        }}])

    async def runner(current, prompt, task):
        if scoped_source:
            assert current.evidence_sources["xiaohongshu"] in forks
            assert current.evidence_sources["xiaohongshu"] is not shared.evidence_sources["xiaohongshu"]
        else:
            assert current.evidence_sources["xiaohongshu"] is shared.evidence_sources["xiaohongshu"]
        target = json.loads(task)["context"]["target_place"]
        received.append((target, current.input_note_ids))
        await asyncio.sleep(0)
        for number in (1, 2):
            await current.execute(lambda number=number: value(note(number)))
        return current.result().model_dump_json()

    async def exercise():
        from dataclasses import replace
        storage = StorageRepository(tmp_path)
        storage.initialize()
        service = VerificationService(storage, extract, subgraphs={"fact": build_fact_subgraph()},
                                      capabilities=replace(shared, llm=model, search=runner))
        results = await asyncio.gather(*(service.run(VerificationInput(
            target_place=f"request-{number}", text="停车场开放。", link=[note(number)["url"]],
        )) for number in (1, 2)))
        assert {target: ids for target, ids in received} == {
            "request-1": frozenset({note_id(1)}), "request-2": frozenset({note_id(2)})}
        assert [result.status for result in results] == ["completed", "completed"]
        assert [result.subgraph_results["fact"].findings[0].evidence[0].url for result in results] == [
            note(2)["url"], note(1)["url"]]
        assert service.capabilities.input_urls == shared.input_urls == ()
        if scoped_source:
            assert len(forks) == 2 and forks[0] is not forks[1]
    asyncio.run(exercise())


def test_twenty_failed_reads_share_budget_without_consuming_query_slots():
    async def exercise():
        current = session()
        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError("页面读取失败")

        for _ in range(21):
            result = await current.execute(broken)
        assert result["stopped"] and len(calls) == current.round.tool_calls == 20
        assert current.round.queries == 0 and current.round.results_per_query == []
    asyncio.run(exercise())


def test_two_rounds_allow_thirty_evidence_and_generated_schema_matches():
    rows = [FactEvidence.model_validate(note(number) | {
        "evidence_id": f"e{number}", "retrieved_at": datetime.now(timezone.utc),
    }) for number in range(1, 31)]
    history = FactClaimState(plan=plan(), evidence=rows, rounds=[
        FactRoundState(round_number=1, tool_calls=17, queries=2, results_per_query=[10, 10], new_evidence_count=15),
        FactRoundState(round_number=2, tool_calls=17, queries=2, results_per_query=[10, 10], new_evidence_count=15),
    ])
    assert len(history.evidence) == 30
    assert SearchResult.model_json_schema()["properties"]["evidence"]["maxItems"] == 15
    assert FactClaimState.model_json_schema()["properties"]["evidence"]["maxItems"] == 30
    assert VerificationCapabilities().subgraph_timeout_seconds == 300


def test_slow_source_returns_partial_material_before_search_deadline(monkeypatch):
    """A bulk source must leave time for the Agent to inspect results and finish."""
    mock_web(monkeypatch)

    class SlowSource:
        async def search(self, query, *, execute, deadline_at, **kwargs):
            async with asyncio.timeout((deadline_at - datetime.now(timezone.utc)).total_seconds()):
                await execute(lambda: value([note(1)]), query=True)
                await execute(lambda: value(note(1)))
                await asyncio.sleep(30)

    async def exercise():
        current = session(evidence_sources={"xiaohongshu": SlowSource()})
        current.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=11)
        tools = create_tools(current, object())
        result = await asyncio.wait_for(action(tools, "search_web", query="停车场"), timeout=2)
        assert len(result["crawler_evidence"]) == 1
        assert "TimeoutError" in result["crawler_error"]
        assert not result["deadline_reached"] and result["remaining_budget"]["time_seconds"] > 8
        finished = await tools.registry.execute_action("done", {})
        assert finished.is_done and finished.success
        assert len(SearchResult.model_validate_json(finished.extracted_content).evidence) == 1

    asyncio.run(exercise())
