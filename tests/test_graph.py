"""通过真实 LangGraph 验证主图编排，提取与证据来源使用测试替身。"""

import asyncio

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
import pytest

from backend.extraction.models import ClaimExtractionResult
from backend.storage.repository import StorageRepository
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import (
    ClaimFinding, Evidence, PlaceReference, SubgraphResult, VerificationInput,
)
from backend.verification.service import VerificationService
from backend.verification.state import SubgraphState


def subgraph(name, handler):
    builder = StateGraph(SubgraphState, context_schema=VerificationCapabilities)
    builder.add_node("check", handler)
    builder.add_edge(START, "check")
    builder.add_edge("check", END)
    return builder.compile(name=name)


def service(tmp_path, extractor, **kwargs):
    storage = StorageRepository(tmp_path)
    storage.initialize()
    return VerificationService(storage, extractor, **kwargs)


def extracted():
    return ClaimExtractionResult(target_place="公园", claims=[
        {"claim_id": "a", "type": "FACT", "content": "免费开放。", "sources": [
            {"source_type": "TEXT", "source_ref": None, "source_text": "免费开放"},
        ]},
        {"claim_id": "b", "type": "CROWD", "content": "周末游客少。", "sources": [
            {"source_type": "TEXT", "source_ref": None, "source_text": "周末游客少"},
        ]},
    ])


def test_entire_batch_reaches_parallel_subgraphs_and_joins_once(tmp_path):
    async def exercise():
        extraction_calls = []
        received = {}
        all_entered = asyncio.Event()

        async def extract(*args):
            extraction_calls.append(args)
            return extracted()

        def handler(name):
            async def check(state: SubgraphState):
                received[name] = [(claim.claim_id, claim.content) for claim in state["claims"]]
                if len(received) == 3:
                    all_entered.set()
                # 三个分支都进入后才返回，可检测分发是否被串行执行。
                await asyncio.wait_for(all_entered.wait(), timeout=2)
                state["claims"][0].content = "分支内部修改"
                return {"result": SubgraphResult(
                    graph_name=name, status="completed", selected_claim_ids=["claim_002"],
                    findings=[
                        ClaimFinding(claim_id="claim_002", summary=f"{name} 发现一"),
                        ClaimFinding(claim_id="claim_002", summary=f"{name} 发现二"),
                    ],
                )}
            return check

        names = ("fact", "route", "new_category")
        verification = service(tmp_path, extract, subgraphs={name: subgraph(name, handler(name)) for name in names})
        updates = [update async for update in verification.graph.astream(
            {"request": VerificationInput(target_place="公园", text="免费开放，周末游客少")},
            context=verification.capabilities, stream_mode="updates",
        )]
        run = next(update["assemble_result"]["result"] for update in updates if "assemble_result" in update)
        assert len(extraction_calls) == 1
        assert all(batch == [("claim_001", "免费开放。"), ("claim_002", "周末游客少。")]
                   for batch in received.values())
        assert sum("collect_results" in update for update in updates) == 1
        assert sum("assemble_result" in update for update in updates) == 1
        assert list(run.subgraph_results) == list(names)
        assert all(len(result.findings) == 2 for result in run.subgraph_results.values())
        assert run.claims[0].content == "免费开放。"
        assert run.status == "completed"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["exception", "invalid_reference", "timeout"])
def test_bad_subgraph_preserves_other_results(tmp_path, failure):
    async def extract(*args):
        return extracted()

    async def good(state: SubgraphState):
        return {"result": SubgraphResult(graph_name="good", status="completed", selected_claim_ids=["claim_001"])}

    async def bad(state: SubgraphState):
        if failure == "exception":
            raise RuntimeError("测试来源不可用")
        if failure == "timeout":
            await asyncio.sleep(10)
        return {"result": SubgraphResult(graph_name="bad", status="completed", selected_claim_ids=["unknown"])}

    verification = service(
        tmp_path, extract, subgraphs={"good": subgraph("good", good), "bad": subgraph("bad", bad)},
        capabilities=VerificationCapabilities(subgraph_timeout_seconds=0.5),
    )
    run = asyncio.run(verification.run(VerificationInput(target_place="公园", text="免费开放，周末游客少")))
    assert run.status == "partial"
    assert run.subgraph_results["good"].status == "completed"
    assert run.subgraph_results["bad"].status == "failed"
    assert run.subgraph_results["bad"].error
    assert len(run.claims) == 2


def test_empty_claims_skip_context_and_subgraphs(tmp_path):
    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[])

    async def unexpected(*args):
        pytest.fail("空主张不应调用地点解析或子图")

    verification = service(
        tmp_path, extract, subgraphs={"unused": subgraph("unused", unexpected)},
        capabilities=VerificationCapabilities(place_resolver=unexpected),
    )
    run = asyncio.run(verification.run(VerificationInput(target_place="公园")))
    assert run.status == "no_claims"
    assert run.claims == []
    assert run.subgraph_results == {}


def test_default_registry_runs_fact_and_keeps_other_placeholders(tmp_path):
    async def extract(*args):
        return extracted()

    calls = []

    async def skip_fact(prompt, task):
        calls.append(task)
        return "[]"

    verification = service(tmp_path, extract, capabilities=VerificationCapabilities(fact_llm=skip_fact))
    run = asyncio.run(verification.run(VerificationInput(target_place="公园", text="免费开放，周末游客少")))
    assert run.status == "partial"
    assert len(calls) == 1
    assert run.subgraph_results["fact"].status == "skipped"
    assert set(run.subgraph_results) == {"fact", "route", "crowd", "experience"}
    assert all(run.subgraph_results[name].status == "not_implemented"
               for name in ("route", "crowd", "experience"))
    with pytest.raises(NotImplementedError, match="尚未接入"):
        asyncio.run(verification.capabilities.evidence_sources["web_search"].search("公园"))


def test_place_and_evidence_dependencies_reach_subgraph_runtime(tmp_path):
    calls = []

    class TestSearch:
        async def search(self, query):
            calls.append(query)
            return [Evidence(source="web_search", content="测试证据", url="https://example.com/evidence")]

    async def resolve(name):
        return PlaceReference(name=name, reference="test-place")

    async def extract(*args):
        return extracted()

    async def check(state: SubgraphState, runtime: Runtime[VerificationCapabilities]):
        assert state["context"].resolved_place.reference == "test-place"
        evidence = await runtime.context.evidence_sources["web_search"].search("公园开放信息")
        return {"result": SubgraphResult(
            graph_name="custom", status="completed", selected_claim_ids=["claim_001"],
            findings=[ClaimFinding(claim_id="claim_001", summary="测试发现", evidence=evidence)],
        )}

    verification = service(
        tmp_path, extract, subgraphs={"custom": subgraph("custom", check)},
        capabilities=VerificationCapabilities(place_resolver=resolve, evidence_sources={"web_search": TestSearch()}),
    )
    run = asyncio.run(verification.run(VerificationInput(target_place="公园", text="免费开放，周末游客少")))
    assert run.status == "completed"
    assert calls == ["公园开放信息"]
    assert run.subgraph_results["custom"].findings[0].evidence[0].url == "https://example.com/evidence"
