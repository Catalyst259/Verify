"""Provider JSON mode must preserve the category array contract and reject malformed output."""

import asyncio
import json

import httpx
from openai import AsyncOpenAI
import pytest

from backend.verification.subgraphs.facts import llm
from test_fact_workflow import assessment, make_plan, read, run


def provider(monkeypatch, content, finish_reason="stop"):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content(body) if callable(content) else content}}]})

    monkeypatch.setattr(llm, "load_config", lambda: {
        "api_key": "test-only", "base_url": "https://model.test/v1", "model": "deepseek-flash", "timeout_seconds": 10})
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    return calls


@pytest.mark.parametrize("items", [[], [{"claim_id": "c0", "text": '正文含 "引号" 和\n换行'}]])
def test_deepseek_json_mode_unwraps_arrays_without_changing_content(monkeypatch, items):
    calls = provider(monkeypatch, json.dumps({"items": items}, ensure_ascii=False))
    result = asyncio.run(llm.complete("category instructions", "business state"))
    assert json.loads(result) == items
    assert len(calls) == 1
    body = calls[0]
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["messages"][0]["content"].startswith("category instructions")
    assert '{"items": []}' in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "business state"}


@pytest.mark.parametrize("content,finish_reason", [
    ('{"items": []}', "length"), ("", "stop"), ("[]", "stop"), ("{}", "stop"),
    ('{"items": {}}', "stop"), ('{"items": [], "extra": []}', "stop"),
    ('{"items": [}', "stop"), ('{"items": [NaN]}', "stop"),
])
def test_invalid_or_truncated_envelope_is_not_repaired_or_retried(monkeypatch, content, finish_reason):
    calls = provider(monkeypatch, content, finish_reason)
    with pytest.raises(ValueError):
        asyncio.run(llm.complete("category instructions", "business state"))
    assert len(calls) == 1


def test_deepseek_json_mode_plan_and_validate_keep_schema_checks(monkeypatch):
    def content(body):
        assert body["response_format"] == {"type": "json_object"}
        state = json.loads(body["messages"][1]["content"])
        if body["messages"][0]["content"].startswith("# Fact Plan"):
            return json.dumps({"items": [make_plan("c0")]})
        return json.dumps({"items": [{"claim_id": "c0", "assessment":
            assessment(state["claim_states"]["c0"], sufficient=True)}]})

    calls = provider(monkeypatch, content)

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(llm.complete, search)
    assert result.status == "completed"
    assert result.findings[0].assessment.verdict == "SUPPORTED"
    assert len(calls) == 2


def test_deepseek_invalid_fact_type_is_corrected_at_plan_boundary(monkeypatch):
    def content(body):
        state = json.loads(body["messages"][1]["content"])
        if body["messages"][0]["content"].startswith("# Fact Plan"):
            item = make_plan("c0")
            if "planning_feedback" not in state:
                item["fact_type"] = "FACT"
            else:
                assert state["planning_feedback"]["c0"]["rejected_plan"]["fact_type"] == "FACT"
            return json.dumps({"items": [item]})
        return json.dumps({"items": [{"claim_id": "c0", "assessment":
            assessment(state["claim_states"]["c0"], sufficient=True)}]})

    calls = provider(monkeypatch, content)

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(llm.complete, search)
    assert result.status == "completed" and len(calls) == 3
    assert all(body["response_format"] == {"type": "json_object"} for body in calls)
    assert result.findings[0].assessment.verdict == "SUPPORTED"
