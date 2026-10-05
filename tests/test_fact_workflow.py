"""真实 Fact 图的状态推进、模型边界、工具预算和失败收束。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
from openai import AsyncOpenAI
import pytest

from backend.extraction.models import Claim
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationContext
from backend.verification.subgraphs.facts import llm
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.facts.model import FactPlan
from backend.verification.subgraphs.facts.search import SearchSession, create_tools
from backend.verification.subgraphs.facts.state import FactClaimState


def make_plan(claim_id):
    return {"claim_id": claim_id, "fact_type": "FACILITY", "target": "公园", "time_scope": "当前",
            "questions": ["停车场是否对游客开放？"],
            "evidence_strategy": [{"priority": 1, "source_type": "WEB", "purpose": "核对开放状态"}]}


def assessment(claim, sufficient=False):
    evidence = claim["evidence"]
    return {
        "target": claim["plan"]["target"], "time_scope": claim["plan"]["time_scope"],
        "verdict": "SUPPORTED" if sufficient else "UNVERIFIED", "confidence": 0.9 if sufficient else None,
        "evidence_sufficient": sufficient, "reason": "已确认开放。" if sufficient else "尚缺开放说明。",
        "supporting_evidence": [evidence[-1]["evidence_id"]] if sufficient else [],
        "context_evidence": [item["evidence_id"] for item in evidence],
        "dimensions": dict.fromkeys(["authority", "directness", "recency", "context_match", "independence"]),
        "remaining_gaps": [] if sufficient else [
            {"question": "是否对游客开放？", "preferred_source": "WEB", "reason": "缺少开放说明。"}],
    }


def inputs(count=1):
    return {"claims": [Claim(claim_id=f"c{i}", type="EXPERIENCE", content="公园有停车场。", sources=[
                           {"source_type": "TEXT", "source_ref": None, "source_text": "公园有停车场。"}])
                       for i in range(count)],
            "context": VerificationContext(target_place="公园", checked_at=datetime.now(timezone.utc))}


def run(model, search=None, count=1, timeout=10):
    return asyncio.run(build_fact_subgraph().ainvoke(inputs(count), context=VerificationCapabilities(
        fact_llm=model, fact_search=search, subgraph_timeout_seconds=timeout,
    )))["result"]


async def read(session, text="停车场向游客开放。"):
    async def operation():
        return {"source": "公园公告", "content": text, "url": "https://example.com/park", "source_type": "WEB"}
    return await session.execute(operation)


def test_two_rounds_preserve_completed_claims_evidence_and_inject_instructions():
    calls, searches = [], []

    async def model(prompt, task):
        state = json.loads(task)
        step = "plan" if prompt.startswith("# Fact Plan") else "validate"
        assert ("# PlanSkill" in prompt) == (step == "plan")
        assert "输出 JSON Schema" in prompt
        assert set(state) == {"claims", "context", "claim_states", "active_claim_ids", "deadline_at"}
        calls.append((step, state))
        if step == "plan":
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        return json.dumps([{"claim_id": cid, "assessment": assessment(
            state["claim_states"][cid], sufficient=cid == "c1" or len(state["claim_states"][cid]["rounds"]) == 2,
        )} for cid in state["active_claim_ids"]])

    async def search(session, prompt, task):
        data = json.loads(task)
        assert prompt.startswith("# Fact Search") and "# PlanSkill" not in prompt
        assert data["context"]["target_place"] == "公园"
        assert data["remaining_budget"]["tool_calls"] == 5
        searches.append((session.claim_id, data["round_number"]))
        await read(session, f"第 {data['round_number']} 轮的原文")
        return session.result().model_dump_json()

    result = run(model, search, count=2)
    assert result.status == "completed"
    assert [step for step, _ in calls] == ["plan", "validate", "plan", "validate"]
    assert calls[2][1]["active_claim_ids"] == ["c0"]
    assert calls[2][1]["claim_states"]["c1"]["assessment"]["verdict"] == "SUPPORTED"
    assert sorted(searches) == [("c0", 1), ("c0", 2), ("c1", 1)]
    first, second = result.findings
    assert len(first.evidence) == 2 and len(second.evidence) == 1
    assert set(first.assessment.context_evidence) == {item.evidence_id for item in first.evidence}
    assert len({item.evidence_id for finding in result.findings for item in finding.evidence}) == 3


@pytest.mark.parametrize("output,status", [("[]", "skipped"), ("invalid", "failed"),
                                          (json.dumps([make_plan("unknown")]), "failed"),
                                          (json.dumps([make_plan("c0")] * 2), "failed")])
def test_empty_or_invalid_plan_does_not_search(output, status):
    calls = []

    async def model(*args):
        calls.append(args)
        return output

    async def unexpected(*args):
        pytest.fail("无有效计划不能搜索")

    result = run(model, unexpected)
    assert result.status == status and len(calls) == 1
    assert bool(result.error) == (status == "failed")
    assert run(model, unexpected, count=0).status == "skipped"
    assert len(calls) == 1


@pytest.mark.parametrize("problem", ["scope", "reference", "missing", "json"])
def test_invalid_validation_keeps_selected_claim_and_material(problem):
    async def model(prompt, task):
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        if problem == "json":
            return "broken"
        if problem == "missing":
            return "[]"
        result = assessment(json.loads(task)["claim_states"]["c0"], sufficient=True)
        if problem == "scope":
            result["time_scope"] = "其他时间"
        else:
            result["supporting_evidence"] = ["invented"]
        return json.dumps([{"claim_id": "c0", "assessment": result}])

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "failed" and result.selected_claim_ids == ["c0"]
    assert result.findings[0].assessment is None and result.findings[0].error
    assert len(result.findings[0].evidence) == 1


@pytest.mark.parametrize("failure", ["plan", "validate", "search", "timeout", "tamper"])
def test_second_round_failure_preserves_previous_assessment_and_evidence(failure):
    previous = []

    async def model(prompt, task):
        state = json.loads(task)
        claim = state["claim_states"].get("c0")
        if prompt.startswith("# Fact Plan"):
            item = make_plan("c0")
            if claim and failure == "plan":
                item["target"] = "另一地点"
            return json.dumps([item])
        if len(claim["rounds"]) == 2 and failure == "validate":
            return "invalid"
        item = assessment(claim)
        previous.append(item)
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session, f"第 {session.round.round_number} 轮")
        if session.round.round_number == 2:
            if failure == "search":
                raise RuntimeError("浏览器断开")
            if failure == "timeout":
                await asyncio.sleep(10)
            if failure == "tamper":
                payload = session.result().model_dump(mode="json")
                payload["evidence"][0]["content"] = "模型编造正文"
                return json.dumps(payload)
        return session.result().model_dump_json()

    result = run(model, search, timeout=0.3 if failure == "timeout" else 10)
    assert result.status == "partial"
    finding = result.findings[0]
    assert finding.error and finding.assessment.verdict == "UNVERIFIED"
    assert finding.evidence[0].content == "第 1 轮"
    if failure in {"plan", "validate", "timeout"}:
        assert finding.assessment.context_evidence == previous[0]["context_evidence"]


def test_empty_search_stops_after_two_rounds_and_missing_capability_skips_retry():
    calls = []

    async def model(prompt, task):
        calls.append(prompt)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        return json.dumps([{"claim_id": "c0", "assessment": assessment(json.loads(task)["claim_states"]["c0"])}])

    async def empty(session, *args):
        return session.result().model_dump_json()

    result = run(model, empty)
    assert len(calls) == 4 and result.status == "completed"
    assert result.findings[0].assessment.remaining_gaps and result.findings[0].error is None
    calls.clear()
    result = run(model)
    assert len(calls) == 2 and result.status == "completed"
    assert "未提供搜索能力" in result.notes[0]


def test_expired_workflow_does_not_start_model_or_browser():
    async def unexpected(*args):
        pytest.fail("截止后不能启动外部调用")

    result = run(unexpected, unexpected, timeout=0)
    assert result.status == "failed" and "截止时间" in result.error


def test_recoverable_search_error_is_not_a_failed_finding():
    async def model(prompt, task):
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        claim = json.loads(task)["claim_states"]["c0"]
        return json.dumps([{"claim_id": "c0", "assessment": assessment(claim, sufficient=True)}])

    async def search(session, *args):
        async def unavailable():
            raise RuntimeError("另一个页面不可用")
        await read(session)
        await session.execute(unavailable)
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "completed" and result.findings[0].error is None
    assert "另一个页面不可用" in result.notes[0]


def new_session():
    return SearchSession(FactClaimState(plan=FactPlan.model_validate(make_plan("c0"))),
                         datetime.now(timezone.utc) + timedelta(seconds=10))


def test_tool_budget_counts_failed_attempts_and_blocks_sixth_call():
    async def exercise():
        session = new_session()
        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError("读取失败")

        for _ in range(6):
            result = await session.execute(broken, query=True)
        assert result["stopped"] and len(calls) == 5
        assert session.round.tool_calls == session.round.queries == 5
        assert session.round.results_per_query == [0] * 5
        assert session.result().error
    asyncio.run(exercise())


def test_query_candidates_deduplication_and_changed_page():
    async def exercise():
        session = new_session()

        async def candidates():
            return list(range(10))

        result = await session.execute(candidates, query=True)
        assert result["data"] == list(range(5)) and session.round.results_per_query == [5]
        await read(session)
        assert (await read(session))["data"] == {"duplicate": True}
        await read(session, "更新后的正文")
        assert len(session.evidence) == 2 and session.round.new_evidence_count == 2
        claim = FactClaimState(plan=FactPlan.model_validate(make_plan("c0")),
                               evidence=session.evidence, rounds=[session.round])
        retry = SearchSession(claim, session.deadline_at)
        assert (await read(retry))["data"] == {"duplicate": True}
        assert retry.round.new_evidence_count == 0
    asyncio.run(exercise())


def test_browser_tools_expose_only_metered_search_read_and_done():
    tools = create_tools(new_session(), object())
    assert set(tools.registry.registry.actions) == {"search_web", "read_page", "done"}


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
@pytest.mark.parametrize("content", ["[]", "```json\n[]\n```", " \r\n```\r\n[]\r\n```\r\n "])
def test_openai_call_uses_config_and_preserves_array_protocol(monkeypatch, finish_reason, content):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": finish_reason,
                                                     "message": {"role": "assistant", "content": content}}]})

    monkeypatch.setattr(llm, "load_config", lambda: {"api_key": "test-only", "base_url": "https://model.test/v1",
                                                     "model": "deepseek-flash", "timeout_seconds": 10})
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    if finish_reason == "stop":
        assert asyncio.run(llm.complete("系统指令", "业务状态")) == "[]"
    else:
        with pytest.raises(ValueError, match="完整"):
            asyncio.run(llm.complete("系统指令", "业务状态"))
    request = captured[0]
    assert str(request.url) == "https://model.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-only"
    body = json.loads(request.content)
    assert body["messages"] == [{"role": "system", "content": "系统指令"}, {"role": "user", "content": "业务状态"}]
    assert body["model"] == "deepseek-flash" and "response_format" not in body
    assert body["thinking"] == {"type": "disabled"} and len(captured) == 1


@pytest.mark.parametrize("invalid", [
    None,
    "下面是结果：\n```json\n[]\n```",
    "```json\n[]\n```\n说明",
    "```json\n[]",
    "```python\n[]\n```",
    "```json\n[}\n```",
    "```json\n[]\n```\n```json\n[]\n```",
    "```json\n[{\"claim_id\": \"c0\"}]\n```",
])
def test_fenced_model_response_through_plan_and_validate(monkeypatch, invalid):
    calls, searches = [], []

    def respond(request):
        body = json.loads(request.content)
        step = "plan" if body["messages"][0]["content"].startswith("# Fact Plan") else "validate"
        calls.append(step)
        state = json.loads(body["messages"][-1]["content"])
        if step == "plan":
            payload = [make_plan("c0")]
            payload[0]["questions"] = ['公告是否提及 "json" 或 ``` 标记？']
        else:
            payload = [{"claim_id": "c0", "assessment": assessment(state["claim_states"]["c0"], sufficient=True)}]
        content = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        if invalid is not None and step == "plan":
            content = invalid
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                                                     "message": {"role": "assistant", "content": content}}]})

    monkeypatch.setattr(llm, "load_config", lambda: {"api_key": "test-only", "base_url": "https://model.test/v1",
                                                     "model": "deepseek-flash", "timeout_seconds": 10})
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))

    async def search(session, prompt, task):
        searches.append(session.claim_id)
        assert json.loads(task)["claim_state"]["plan"]["questions"] == ['公告是否提及 "json" 或 ``` 标记？']
        await read(session)
        return session.result().model_dump_json()

    result = run(llm.complete, search)
    if invalid is None:
        assert result.status == "completed", result.error
        assert result.findings[0].assessment.verdict == "SUPPORTED"
        assert calls == ["plan", "validate"] and searches == ["c0"]
    else:
        assert result.status == "failed" and "Plan: ValidationError:" in result.error
        assert calls == ["plan"] and not searches
