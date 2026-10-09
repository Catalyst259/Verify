"""No evidence cannot generate citations; a rejected citation gets one bounded correction."""

import asyncio
import json

import pytest

from backend.verification.capabilities import VerificationCapabilities
from backend.verification.subgraphs.crowd.graph import build_crowd_subgraph
from backend.verification.subgraphs.experience.graph import build_experience_subgraph
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.route.graph import build_route_subgraph
from test_crowd_workflow import make_plan as crowd_plan
from test_experience_workflow import make_plan as experience_plan
from test_fact_workflow import assessment, inputs, make_plan, read, run
from test_route_workflow import make_plan as route_plan
from test_route_workflow import inputs as route_inputs


@pytest.mark.parametrize("build,plan", [
    (build_fact_subgraph, make_plan("c0")),
    (build_crowd_subgraph, crowd_plan("c0")),
    (build_experience_subgraph, experience_plan("c0")),
    (build_route_subgraph, route_plan("c0").model_dump(mode="json")),
])
def test_empty_evidence_never_calls_judge_or_accepts_a_citation(build, plan):
    async def model(prompt, task):
        assert prompt.splitlines()[0].endswith(" Plan"), "Without evidence there is nothing for the judge to assess"
        return json.dumps([plan])

    data = route_inputs() if build is build_route_subgraph else inputs()
    result = asyncio.run(build().ainvoke(data, context=VerificationCapabilities(
        llm=model, search=None, subgraph_timeout_seconds=10)))["result"]
    finding = result.findings[0]
    assert result.status == "completed" and finding.error is None
    assert finding.assessment.verdict == "UNVERIFIED"
    assert finding.assessment.confidence is None and not finding.assessment.evidence_sufficient
    assert not finding.evidence
    assert not (finding.assessment.supporting_evidence + finding.assessment.counter_evidence
                + finding.assessment.context_evidence)


def test_failed_search_keeps_its_error_but_has_a_safe_empty_assessment():
    async def model(prompt, task):
        assert prompt.splitlines()[0].endswith(" Plan")
        return json.dumps([make_plan("c0")])

    async def broken_search(*args):
        raise TimeoutError("取证超时")

    result = run(model, broken_search)
    assert result.status == "partial" and result.findings[0].assessment.verdict == "UNVERIFIED"
    assert "Search: TimeoutError" in result.findings[0].error
    assert "Validate:" not in result.findings[0].error


@pytest.mark.parametrize("repaired", [True, False])
def test_cross_claim_reference_is_corrected_once_without_rejudging_good_claim(repaired):
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.splitlines()[0].endswith(" Plan"):
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        calls.append(state)
        if len(calls) == 2:
            assert state["active_claim_ids"] == ["c0"]
            feedback = state["validation_feedback"]["c0"]
            assert feedback["allowed_evidence_ids"] == [state["claim_states"]["c0"]["evidence"][0]["evidence_id"]]
        results = []
        for cid in state["active_claim_ids"]:
            item = assessment(state["claim_states"][cid], sufficient=True)
            if cid == "c0" and (len(calls) == 1 or not repaired):
                item["supporting_evidence"] = [state["claim_states"]["c1"]["evidence"][0]["evidence_id"]]
            results.append({"claim_id": cid, "assessment": item})
        return json.dumps(results)

    async def search(session, *args):
        await read(session, f"{session.claim_id} 的独立材料")
        return session.result().model_dump_json()

    result = run(model, search, count=2)
    assert len(calls) == 2
    good, other = result.findings
    assert other.error is None and other.assessment.verdict == "SUPPORTED"
    assert len(good.evidence) == len(other.evidence) == 1
    if repaired:
        assert result.status == "completed" and good.error is None
        assert good.assessment.supporting_evidence == [good.evidence[0].evidence_id]
        assert any("引用纠正成功" in note for note in result.notes)
    else:
        assert result.status == "partial" and good.assessment is None
        assert "未取得的证据" in good.error


def test_correction_timeout_does_not_extend_budget_or_lose_other_result():
    calls, deadlines = [], []

    async def model(prompt, task):
        state = json.loads(task)
        deadlines.append(state["deadline_at"])
        if prompt.splitlines()[0].endswith(" Plan"):
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        calls.append(state)
        if len(calls) == 2:
            try:
                await asyncio.sleep(10)
            finally:
                calls.append("cancelled")
        results = []
        for cid in state["active_claim_ids"]:
            item = assessment(state["claim_states"][cid], sufficient=True)
            if cid == "c0":
                item["supporting_evidence"] = ["invented"]
            results.append({"claim_id": cid, "assessment": item})
        return json.dumps(results)

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search, count=2, timeout=0.3)
    assert calls[-1] == "cancelled" and len(set(deadlines)) == 1
    assert result.status == "partial"
    assert result.findings[1].assessment.verdict == "SUPPORTED" and result.findings[1].error is None
    assert "TimeoutError" in result.findings[0].error
    assert len(result.findings[0].evidence) == 1


def test_empty_claim_survives_an_unrelated_model_failure():
    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        assert state["active_claim_ids"] == ["c1"]
        return "invalid"

    async def search(session, *args):
        if session.claim_id == "c1":
            await read(session)
        return session.result().model_dump_json()

    result = run(model, search, count=2)
    assert result.status == "partial"
    assert result.findings[0].assessment.verdict == "UNVERIFIED" and result.findings[0].error is None
    assert result.findings[1].assessment is None and "Validate:" in result.findings[1].error


def test_corrected_judgment_preserves_a_partial_search_error():
    validations = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        validations.append(state)
        item = assessment(state["claim_states"]["c0"], sufficient=True)
        if len(validations) == 1:
            item["supporting_evidence"] = ["invented"]
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session)
        raise TimeoutError("取证超时")

    result = run(model, search)
    assert len(validations) == 2 and result.status == "partial"
    assert result.findings[0].assessment.verdict == "SUPPORTED"
    assert "Search: TimeoutError" in result.findings[0].error
    assert "Validate:" not in result.findings[0].error
