"""Native search finishes from its ledger; model output cannot rewrite evidence."""

import asyncio

import pytest
from test_fact_workflow import make_plan, new_session, read

from backend.verification.subgraphs.facts.model import FactPlan, SearchResult
from backend.verification.subgraphs.facts.prompts import load_system_prompt
from backend.verification.subgraphs.facts.search import SearchSession, create_tools
from backend.verification.subgraphs.facts.state import FactClaimState


async def finish(session):
    return await create_tools(session, object()).registry.execute_action("done", {})


def test_done_returns_unique_current_round_ledger_and_real_errors_without_using_budget():
    async def exercise():
        session = new_session()
        await read(session)
        assert (await read(session))["data"] == {"duplicate": True}

        async def broken():
            raise RuntimeError("本次页面读取失败")

        await session.execute(broken)
        before = session.round.model_dump()
        result = await finish(session)
        assert result.is_done is True and result.success is True
        output = SearchResult.model_validate_json(result.extracted_content)
        assert output == session.result() and len(output.evidence) == 1
        assert "本次页面读取失败" in output.error
        assert session.round.model_dump() == before
        assert session.round.new_evidence_count == 1
    asyncio.run(exercise())


def test_duplicate_second_round_returns_empty_new_ledger_and_keeps_history():
    async def exercise():
        first = new_session()
        await read(first)
        history = FactClaimState(plan=FactPlan.model_validate(make_plan("c0")),
                                 evidence=first.evidence, rounds=[first.round])
        second = SearchSession(history, first.deadline_at)
        assert (await read(second))["data"] == {"duplicate": True}
        output = SearchResult.model_validate_json((await finish(second)).extracted_content)
        assert output.evidence == [] and output.error is None
        assert second.round.round_number == 2 and second.round.new_evidence_count == 0
        merged = FactClaimState.model_validate(history.model_dump() | {
            "evidence": [*history.evidence, *output.evidence], "rounds": [*history.rounds, second.round],
        })
        assert merged.evidence == history.evidence and len(merged.evidence) == 1
        with pytest.raises(ValueError, match="未读取"):
            second.result(SearchResult(claim_id="c0", evidence=history.evidence, error=None).model_dump_json())
    asyncio.run(exercise())


@pytest.mark.parametrize("params", [
    {"success": True}, {"data": {"evidence": []}}, {"evidence": []}, {"error": "模型杜撰的错误"},
])
def test_done_rejects_model_result_fields_without_changing_the_ledger(params):
    async def exercise():
        session = new_session()
        await read(session)
        before = session.result().model_dump_json(), session.round.model_dump()
        tools = create_tools(session, object())
        with pytest.raises(RuntimeError, match="Invalid parameters"):
            await tools.registry.execute_action("done", params)
        assert (session.result().model_dump_json(), session.round.model_dump()) == before
        assert SearchResult.model_validate_json((await finish(session)).extracted_content) == session.result()
    asyncio.run(exercise())


def test_search_prompt_requests_only_the_empty_done_action():
    prompt = load_system_prompt("search")
    assert '{"done": {}}' in prompt and "## 输出 JSON Schema" not in prompt
    assert '"properties"' not in prompt and '"evidence_id"' not in prompt
    assert "## 输出 JSON Schema" in load_system_prompt("plan")
    assert "## 输出 JSON Schema" in load_system_prompt("validate")
