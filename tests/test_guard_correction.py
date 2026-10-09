"""Category guard failures get one correction without weakening their evidence rules."""

import asyncio
from dataclasses import replace
from datetime import date
import json

import pytest

from backend.verification.capabilities import VerificationCapabilities
from backend.common.errors import ModelOutputError
from backend.verification.subgraphs.crowd.graph import CROWD_SPEC
from backend.verification.subgraphs.crowd.model import ValidateResult
from backend.verification.subgraphs.skeleton import build_category_subgraph
from test_crowd_workflow import inputs, make_plan, planned, read, unsupported
from test_route_workflow import FakeRouting, assessment, make_plan as route_plan, run as run_route


ALLOWLIST_MARKER = "本次活动主张可引用的 evidence_id 清单（按 claim_id）：\n"


def citation_allowlist(prompt):
    return json.loads(prompt.split(ALLOWLIST_MARKER, 1)[1].splitlines()[0])


@pytest.mark.parametrize("repaired", [True, False])
@pytest.mark.parametrize("publication,reason", [
    (None, "发布时间未知"),
    (date(2026, 8, 11), "场景不符"),
    (date(2019, 8, 17), "超出时效"),
])
def test_crowd_guard_correction_preserves_bodies_scope_good_rows_and_deadline(publication, reason, repaired):
    validations = []
    rejected = None

    async def model(prompt, task):
        nonlocal rejected
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        validations.append(state)
        allowed = citation_allowlist(prompt)
        assert allowed == {cid: [state["claim_states"][cid]["evidence"][0]["evidence_id"]]
                           for cid in state["active_claim_ids"]}
        if len(validations) == 1:
            assert set(state) == {"claims", "context", "claim_states", "active_claim_ids", "deadline_at"}
        else:
            assert state["active_claim_ids"] == ["c0"]
            feedback = state["validation_feedback"]["c0"]
            assert reason in feedback["error"]
            assert feedback["allowed_evidence_ids"] == allowed["c0"]
            assert feedback["rejected_result"] == ValidateResult.model_validate(rejected).model_dump(mode="json")
            assert feedback["errors"][0]["loc"] == ["assessment"]
        output = []
        for cid in state["active_claim_ids"]:
            item = planned(allowed[cid])
            if cid == "c0" and len(validations) == 2 and repaired:
                item = unsupported(context_evidence=allowed[cid]) | {"reason": f"材料{reason}，不能支持周末结论。"}
            row = {"claim_id": cid, "assessment": item}
            if cid == "c0" and len(validations) == 1:
                rejected = row
            output.append(row)
        return json.dumps(output)

    async def search(session, *args):
        await read(session, published_at=publication if session.claim_id == "c0" else date(2026, 8, 15))
        return session.result().model_dump_json()

    # This test exercises judgment repair, not the independently tested optional search retry.
    spec = replace(CROWD_SPEC, needs_more=lambda *args: False)
    result = asyncio.run(build_category_subgraph(spec).ainvoke(inputs(2), context=VerificationCapabilities(
        llm=model, search=search, subgraph_timeout_seconds=10)))["result"]
    assert len(validations) == 2
    assert validations[0]["deadline_at"] == validations[1]["deadline_at"]
    bad, good = result.findings
    assert len(bad.evidence) == len(good.evidence) == 1
    assert good.error is None and good.assessment.verdict == "SUPPORTED"
    assert bad.evidence[0].published_at == publication
    if repaired:
        assert result.status == "completed" and bad.error is None
        assert bad.assessment.verdict == "UNVERIFIED" and bad.assessment.confidence is None
        assert not bad.assessment.evidence_sufficient
        assert not bad.assessment.supporting_evidence and not bad.assessment.counter_evidence
        assert bad.assessment.context_evidence == [bad.evidence[0].evidence_id]
        assert bad.assessment.scenario == "周末"
    else:
        assert result.status == "partial" and bad.assessment is None and reason in bad.error


def test_correcting_crowd_guard_does_not_clear_an_existing_search_failure():
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            return json.dumps([make_plan("c0")])
        calls.append(state)
        ids = citation_allowlist(prompt)["c0"]
        item = planned(ids) if len(calls) == 1 else unsupported(context_evidence=ids)
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session)
        raise TimeoutError("取证超时")

    result = asyncio.run(build_category_subgraph(CROWD_SPEC).ainvoke(inputs(), context=VerificationCapabilities(
        llm=model, search=search, subgraph_timeout_seconds=10)))["result"]
    finding = result.findings[0]
    assert len(calls) == 2 and result.status == "partial"
    assert finding.assessment.verdict == "UNVERIFIED" and len(finding.evidence) == 1
    assert "Search: TimeoutError" in finding.error and "Validate:" not in finding.error


@pytest.mark.parametrize("repaired", [True, False])
def test_route_initial_allowlist_and_measured_guard_correction_never_replace_real_measurement(repaired):
    plan = route_plan("c0")
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json")])
        calls.append(state)
        assert citation_allowlist(prompt) == {"c0": ["c0-r1-0"]}
        if len(calls) == 2:
            feedback = state["validation_feedback"]["c0"]
            assert "实测值不在" in feedback["error"]
            assert feedback["rejected_result"]["assessment"]["measured_seconds"] == 700
        measured = 300 if len(calls) == 2 and repaired else 700
        return json.dumps([{"claim_id": "c0", "assessment": assessment(plan, "MATCHED", measured)}])

    result = run_route(model, FakeRouting(duration=300))
    finding = result.findings[0]
    assert len(calls) == 2 and calls[0]["deadline_at"] == calls[1]["deadline_at"]
    assert finding.evidence[0].content.startswith("pedestrian 实测 300 秒")
    if repaired:
        assert result.status == "completed" and finding.error is None
        assert finding.assessment.measured_seconds == 300
        assert finding.assessment.claimed_seconds == plan.claimed_seconds
        assert finding.assessment.supporting_evidence == ["c0-r1-0"]
    else:
        assert result.status == "failed" and finding.assessment is None
        assert "实测值不在" in finding.error


def test_category_guard_correction_timeout_uses_original_deadline_and_keeps_evidence():
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            return json.dumps([make_plan("c0")])
        calls.append(state)
        if len(calls) == 2:
            try:
                await asyncio.sleep(10)
            finally:
                calls.append("cancelled")
        ids = [state["claim_states"]["c0"]["evidence"][0]["evidence_id"]]
        return json.dumps([{"claim_id": "c0", "assessment": planned(ids)}])

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = asyncio.run(build_category_subgraph(CROWD_SPEC).ainvoke(inputs(), context=VerificationCapabilities(
        llm=model, search=search, subgraph_timeout_seconds=0.3)))["result"]
    assert len(calls) == 3 and calls[-1] == "cancelled"
    assert calls[0]["deadline_at"] == calls[1]["deadline_at"]
    assert result.status == "failed" and len(result.findings[0].evidence) == 1
    assert "Validate correction: TimeoutError" in result.findings[0].error


@pytest.mark.parametrize("second_failure", [None, "output", "citation"])
def test_incomplete_validate_uses_only_one_correction_and_keeps_real_evidence(second_failure):
    plan = route_plan("c0")
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json")])
        calls.append(state)
        array_schema = json.loads(prompt.split("不能把证据正文、说明或来源拼到 ID 后面。\n", 1)[1].splitlines()[0])
        assert array_schema == {"c0": {"type": "array", "items": {"enum": ["c0-r1-0"]}}}
        if len(calls) == 1 or second_failure == "output":
            raise ModelOutputError("模型未返回完整的有效 JSON")
        feedback = state["validation_feedback"]["c0"]
        assert "重新生成完整判定" in feedback["error"] and "rejected_result" not in feedback
        return json.dumps([{"claim_id": "c0", "assessment": assessment(
            plan, "MATCHED", 300, cited="unknown" if second_failure == "citation" else "c0-r1-0")}])

    result = run_route(model, FakeRouting(duration=300))
    finding = result.findings[0]
    assert len(calls) == 2 and calls[0]["deadline_at"] == calls[1]["deadline_at"]
    assert [e.evidence_id for e in finding.evidence] == ["c0-r1-0"]
    if second_failure is None:
        assert result.status == "completed" and finding.error is None
        assert finding.assessment.supporting_evidence == ["c0-r1-0"]
    else:
        assert result.status == "failed" and finding.assessment is None
        assert "Validate" in finding.error
