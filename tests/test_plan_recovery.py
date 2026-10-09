"""Plan schema errors get one correction; identities, valid rows and the deadline stay fixed."""

import asyncio
import json

import pytest

from backend.common.errors import ModelOutputError
from backend.verification.capabilities import VerificationCapabilities
from backend.extraction.models import Claim
from backend.verification.subgraphs import skeleton
from backend.verification.subgraphs.crowd.graph import build_crowd_subgraph
from backend.verification.subgraphs.experience.graph import build_experience_subgraph
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.route.graph import build_route_subgraph
from test_crowd_workflow import make_plan as crowd_plan
from test_experience_workflow import make_plan as experience_plan
from test_fact_workflow import assessment, inputs, make_plan, read, run
from test_route_workflow import inputs as route_inputs, make_plan as route_plan


async def unexpected_search(*args):
    pytest.fail("A rejected plan must not start evidence collection")


def test_nine_invalid_fact_types_are_corrected_without_replanning_ten_valid_rows():
    bad_indices = {0, 1, 2, 3, 6, 11, 13, 17, 18}
    types = ["OPEN_STATUS", "PRICE_POLICY", "RESERVATION", "ACCESS_POLICY", "FACILITY", "TEMPORARY_EVENT"]
    plans = [make_plan(f"c{i}") | {"fact_type": types[i % len(types)]} for i in range(19)]
    bad_ids = [plans[i]["claim_id"] for i in sorted(bad_indices)]
    calls, searched = [], []

    async def model(prompt, task):
        state = json.loads(task)
        calls.append(state)
        if prompt.startswith("# Fact Plan"):
            if "planning_feedback" not in state:
                return json.dumps([plan | {"fact_type": "FACT"} if i in bad_indices else plan
                                   for i, plan in enumerate(plans)])
            assert state["active_claim_ids"] == bad_ids
            assert set(state["planning_feedback"]) == set(bad_ids)
            assert "Claim.type" in prompt and "fact_type" in prompt
            for cid, feedback in state["planning_feedback"].items():
                assert feedback["rejected_plan"]["claim_id"] == cid
                assert feedback["rejected_plan"]["fact_type"] == "FACT"
                assert feedback["errors"][0]["loc"] == ["fact_type"]
                assert "OPEN_STATUS" in feedback["errors"][0]["msg"]
                assert "input" not in feedback["errors"][0]
            return json.dumps([plan for plan in plans if plan["claim_id"] in bad_ids])
        return json.dumps([{"claim_id": cid, "assessment": assessment(
            state["claim_states"][cid], sufficient=True)} for cid in state["active_claim_ids"]])

    async def search(session, prompt, task):
        state = json.loads(task)
        plan = state["claim_state"]["plan"]
        assert plan == plans[int(session.claim_id[1:])]
        searched.append(session.claim_id)
        await read(session)
        return session.result().model_dump_json()

    data = inputs(19)
    data["claims"] = [Claim.model_validate(claim.model_dump() | {"type": "FACT"}) for claim in data["claims"]]
    result = asyncio.run(build_fact_subgraph().ainvoke(data, context=VerificationCapabilities(
        llm=model, search=search, subgraph_timeout_seconds=10)))["result"]
    assert result.status == "completed", result.error
    assert len(calls) == 3 and len({state["deadline_at"] for state in calls}) == 1
    assert result.selected_claim_ids == [plan["claim_id"] for plan in plans]
    assert set(searched) == set(result.selected_claim_ids)
    assert all(f.assessment.verdict == "SUPPORTED" and f.error is None for f in result.findings)
    assert sum("计划格式纠正成功" in note for note in result.notes) == 9


@pytest.mark.parametrize("build,plan", [
    (build_fact_subgraph, make_plan("c0")),
    (build_crowd_subgraph, crowd_plan("c0")),
    (build_experience_subgraph, experience_plan("c0")),
    (build_route_subgraph, route_plan("c0").model_dump(mode="json")),
])
def test_shared_plan_correction_keeps_category_contracts(build, plan):
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        calls.append(state)
        assert prompt.splitlines()[0].endswith(" Plan")
        if len(calls) == 1:
            return json.dumps([{key: value for key, value in plan.items() if key != "questions"}])
        assert state["planning_feedback"]["c0"]["errors"][0]["type"] == "missing"
        return json.dumps([plan])

    data = route_inputs() if build is build_route_subgraph else inputs()
    result = asyncio.run(build().ainvoke(data, context=VerificationCapabilities(
        llm=model, search=None, subgraph_timeout_seconds=10)))["result"]
    assert len(calls) == 2 and result.status == "completed"
    assert result.findings[0].assessment.verdict == "UNVERIFIED"


@pytest.mark.parametrize("output", [
    [{"claim_id": "unknown", "fact_type": "FACT"}],
    [{"claim_id": "c0", "fact_type": "FACT"}] * 2,
    [{"fact_type": "FACT"}], [{"claim_id": [], "fact_type": "FACT"}],
])
def test_invalid_plan_identity_is_not_sent_for_correction(output):
    calls = []

    async def model(*args):
        calls.append(1)
        return json.dumps(output)

    result = run(model, unexpected_search)
    assert len(calls) == 1 and result.status == "failed" and result.error.startswith("Plan:")


@pytest.mark.parametrize("correction", [
    [make_plan("c0") | {"fact_type": "FACT"}], [], [make_plan("unknown")],
    [make_plan("c0")] * 2, [make_plan("c0"), make_plan("c1")],
])
def test_failed_correction_is_not_retried_or_allowed_to_drop_bad_rows(correction):
    calls = []

    async def model(prompt, task):
        calls.append(json.loads(task))
        if len(calls) == 1:
            return json.dumps([make_plan("c0") | {"fact_type": "FACT"}, make_plan("c1")])
        return json.dumps(correction)

    result = run(model, unexpected_search, count=2)
    assert len(calls) == 2 and calls[1]["active_claim_ids"] == ["c0"]
    assert result.status == "failed" and result.error.startswith("Plan:")


@pytest.mark.parametrize("change", [None, "target", "fact_type"])
def test_corrected_replan_keeps_previous_evidence_and_scope(change):
    plan_calls, searches = [], []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            plan_calls.append(state)
            plan = make_plan("c0")
            if len(plan_calls) == 2:
                plan["fact_type"] = "FACT"
            if len(plan_calls) == 3 and change:
                plan[change] = "其他地点" if change == "target" else "OPEN_STATUS"
            return json.dumps([plan])
        claim = state["claim_states"]["c0"]
        return json.dumps([{"claim_id": "c0", "assessment": assessment(
            claim, sufficient=len(claim["rounds"]) == 2)}])

    async def search(session, *args):
        searches.append(session.round.round_number)
        await read(session, f"第 {session.round.round_number} 轮材料")
        return session.result().model_dump_json()

    result = run(model, search)
    finding = result.findings[0]
    assert len(plan_calls) == 3 and finding.evidence[0].content == "第 1 轮材料"
    assert plan_calls[2]["claim_states"]["c0"]["plan"]["fact_type"] == "FACILITY"
    if change:
        assert searches == [1] and result.status == "partial"
        assert finding.assessment.verdict == "UNVERIFIED" and "改变了原主张" in finding.error
    else:
        assert searches == [1, 2] and result.status == "completed"
        assert len(finding.evidence) == 2 and finding.assessment.verdict == "SUPPORTED"


def test_plan_correction_timeout_uses_the_original_deadline():
    calls, cancelled = [], []

    async def model(prompt, task):
        calls.append(json.loads(task))
        if len(calls) == 2:
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        return json.dumps([make_plan("c0") | {"fact_type": "FACT"}])

    result = run(model, unexpected_search, timeout=0.3)
    assert len(calls) == 2 and cancelled == [True]
    assert calls[0]["deadline_at"] == calls[1]["deadline_at"]
    assert result.status == "failed" and "Plan: TimeoutError:" in result.error


def test_expired_plan_budget_does_not_start_correction(monkeypatch):
    original_remaining = skeleton.remaining
    expired, calls = [], []
    monkeypatch.setattr(skeleton, "remaining", lambda deadline: 0 if expired else original_remaining(deadline))

    async def model(*args):
        calls.append(1)
        expired.append(True)
        return json.dumps([make_plan("c0") | {"fact_type": "FACT"}])

    result = run(model, unexpected_search)
    assert calls == [1] and result.status == "failed" and "TimeoutError" in result.error


def test_correction_keeps_the_initial_plan_selection():
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        calls.append(state)
        if "planning_feedback" not in state:
            return json.dumps([make_plan("c1") | {"fact_type": "FACT"}])
        assert state["active_claim_ids"] == ["c1"]
        return json.dumps([make_plan("c1")])

    result = run(model, count=3)
    assert result.status == "completed" and result.selected_claim_ids == ["c1"] and len(calls) == 2


@pytest.mark.parametrize("error", [ValueError("响应不完整"), TimeoutError("网络超时"), RuntimeError("连接断开")])
def test_transport_failures_are_not_schema_corrections(error):
    calls = []

    async def model(*args):
        calls.append(1)
        raise error

    result = run(model, unexpected_search)
    assert result.status == "failed" and calls == [1] and str(error) in result.error


@pytest.mark.parametrize("selected", [[], ["c1"]])
def test_complete_regeneration_keeps_initial_category_selection_and_does_not_expose_bad_output(selected):
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        calls.append(state)
        if len(calls) == 1:
            raise ModelOutputError("untrusted fragment https://example.test/?xsec_token=secret")
        assert set(state["planning_feedback"]) == {"output_error"}
        assert "xsec_token" not in task and "untrusted fragment" not in task
        return json.dumps([make_plan(cid) for cid in selected])

    result = run(model, count=3)
    assert len(calls) == 2 and calls[0]["deadline_at"] == calls[1]["deadline_at"]
    assert result.selected_claim_ids == selected and result.status == ("completed" if selected else "skipped")


@pytest.mark.parametrize("second", [
    "format", "schema", "unknown", "duplicate", "transport",
])
def test_complete_regeneration_and_schema_correction_share_one_retry(second):
    calls = []

    async def model(prompt, task):
        calls.append(json.loads(task))
        if len(calls) == 1 or second == "format":
            raise ModelOutputError("模型未返回完整的有效 JSON")
        if second == "transport":
            raise TimeoutError("连接超时")
        rows = [make_plan("c0")]
        if second == "schema":
            rows[0]["fact_type"] = "FACT"
        elif second == "unknown":
            rows[0]["claim_id"] = "unknown"
        elif second == "duplicate":
            rows *= 2
        return json.dumps(rows)

    result = run(model, unexpected_search)
    assert result.status == "failed" and len(calls) == 2 and result.error.startswith("Plan:")


def test_schema_correction_does_not_gain_another_retry_when_its_output_is_incomplete():
    calls = []

    async def model(prompt, task):
        calls.append(json.loads(task))
        if len(calls) == 1:
            return json.dumps([make_plan("c0") | {"fact_type": "FACT"}])
        raise ModelOutputError("模型未返回完整的有效 JSON")

    result = run(model, unexpected_search)
    assert result.status == "failed" and len(calls) == 2


def test_complete_regeneration_timeout_uses_original_deadline_and_cancels_call():
    calls, cancelled = [], []

    async def model(prompt, task):
        calls.append(json.loads(task))
        if len(calls) == 1:
            raise ModelOutputError("模型未返回完整的有效 JSON")
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    result = run(model, unexpected_search, timeout=.3)
    assert result.status == "failed" and "TimeoutError" in result.error
    assert len(calls) == 2 and cancelled == [True]
    assert calls[0]["deadline_at"] == calls[1]["deadline_at"]


@pytest.mark.parametrize("change", [None, "missing", "target"])
def test_complete_regeneration_of_replan_preserves_existing_scope_and_material(change):
    calls, searches = [], []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            calls.append(state)
            if len(calls) == 2:
                raise ModelOutputError("模型未返回完整的有效 JSON")
            if len(calls) == 3 and change == "missing":
                return "[]"
            plan = make_plan("c0")
            if len(calls) == 3 and change == "target":
                plan["target"] = "其他地点"
            return json.dumps([plan])
        old = state["claim_states"]["c0"]
        return json.dumps([{"claim_id": "c0", "assessment": assessment(old, sufficient=len(old["rounds"]) == 2)}])

    async def search(session, *args):
        searches.append(session.round.round_number)
        await read(session, f"第 {session.round.round_number} 轮材料")
        return session.result().model_dump_json()

    result = run(model, search)
    finding = result.findings[0]
    assert len(calls) == 3 and len({state["deadline_at"] for state in calls}) == 1
    assert finding.evidence[0].content == "第 1 轮材料"
    if change:
        assert result.status == "partial" and searches == [1]
        assert finding.assessment.verdict == "UNVERIFIED" and finding.error
    else:
        assert result.status == "completed" and searches == [1, 2]
        assert finding.assessment.verdict == "SUPPORTED" and finding.error is None
