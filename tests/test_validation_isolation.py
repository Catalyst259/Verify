"""Invalid model rows cannot discard other claims or turn insufficient evidence into a verdict."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from backend.extraction.models import Claim
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import FactAssessment
from backend.verification.subgraphs.route.graph import ROUTE_SPEC
from backend.verification.subgraphs.route.model import RouteEvidence
from backend.verification.subgraphs.skeleton import build_category_subgraph
from test_fact_workflow import assessment, make_plan, read, run
from test_route_workflow import assessment as route_assessment, inputs as route_inputs, make_plan as route_plan


def test_route_correction_keeps_good_row_when_other_rows_report_insufficient_evidence_with_confidence():
    calls = []
    plans = {f"c{i}": route_plan(f"c{i}") for i in range(3)}

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json") for plan in plans.values()])
        calls.append(state)
        output = []
        for cid in state["active_claim_ids"]:
            measured = 300 if cid == "c0" else None
            item = route_assessment(plans[cid], "MATCHED" if measured else "UNVERIFIED", measured,
                                    cited=state["claim_states"][cid]["evidence"][0]["evidence_id"])
            if len(calls) == 1:
                item["context_evidence"] = ["invented"]
            elif cid != "c0":
                item["confidence"] = 0.0
            output.append({"claim_id": cid, "assessment": item})
        return json.dumps(output)

    async def search(session, *args):
        measured = 300 if session.claim_id == "c0" else None
        session.add(RouteEvidence(evidence_id=f"{session.claim_id}-actual", origin_name="示例站",
                                  destination_name="示例公园", transport_mode="pedestrian",
                                  measured_seconds=measured, distance_meters=1500 if measured else None,
                                  source="map_routing", content="实际地图响应", retrieved_at=datetime.now(timezone.utc)))
        session.update_round(measurements=1)
        return session.result().model_dump_json()

    data = route_inputs()
    data["claims"] = [Claim.model_validate(data["claims"][0].model_dump() | {"claim_id": cid}) for cid in plans]
    spec = replace(ROUTE_SPEC, search_runner=lambda runtime: search)
    result = asyncio.run(build_category_subgraph(spec).ainvoke(data, context=VerificationCapabilities(
        llm=model, search=None, subgraph_timeout_seconds=10)))["result"]
    assert result.status == "completed" and len(calls) == 2
    assert [f.assessment.verdict for f in result.findings] == ["MATCHED", "UNVERIFIED", "UNVERIFIED"]
    assert all(f.error is None and len(f.evidence) == 1 for f in result.findings)
    assert all(f.assessment.confidence is None and not f.assessment.evidence_sufficient for f in result.findings[1:])
    assert all(set(f.assessment.context_evidence) <= {e.evidence_id for e in f.evidence} for f in result.findings)
    assert calls[0]["deadline_at"] == calls[1]["deadline_at"]


def test_schema_error_is_isolated_on_initial_and_corrected_validation():
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        calls.append(state)
        output = []
        for cid in state["active_claim_ids"]:
            item = assessment(state["claim_states"][cid], sufficient=True)
            if cid == "c0" and len(calls) == 1:
                item["supporting_evidence"] = ["invented"]
            if cid == "c1":
                del item["reason"]
            output.append({"claim_id": cid, "assessment": item})
        return json.dumps(output)

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search, count=3)
    assert result.status == "partial" and len(calls) == 2
    assert set(calls[1]["active_claim_ids"]) == {"c0", "c1"}
    assert result.findings[0].assessment.verdict == result.findings[2].assessment.verdict == "SUPPORTED"
    assert result.findings[0].error is result.findings[2].error is None
    assert result.findings[1].assessment is None and "assessment.reason" in result.findings[1].error
    assert "input_value" not in result.findings[1].error and "errors.pydantic.dev" not in result.findings[1].error
    assert all(len(f.evidence) == 1 for f in result.findings)


def test_explicit_insufficient_evidence_is_conservatively_canonicalized_without_weakening_schema():
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        calls.append(state)
        item = assessment(state["claim_states"]["c0"], sufficient=True) | {"evidence_sufficient": False}
        with pytest.raises(ValueError, match="证据不足"):
            FactAssessment.model_validate(item)
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search)
    finding = result.findings[0]
    assert len(calls) == 1 and result.status == "completed" and finding.error is None
    assert finding.assessment.verdict == "UNVERIFIED" and finding.assessment.confidence is None
    assert "暂无法形成结论" in finding.assessment.reason
    assert any("证据不足" in note and "规范" in note for note in result.notes)


@pytest.mark.parametrize("problem", ["scope", "reference", "unknown_verdict", "missing_reason"])
def test_insufficient_canonicalization_does_not_bypass_scope_or_reference_guard(problem):
    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        item = assessment(state["claim_states"]["c0"], sufficient=True) | {"evidence_sufficient": False}
        if problem == "scope":
            item["target"] = "其他地点"
        elif problem == "reference":
            item["context_evidence"] = ["invented"]
        elif problem == "unknown_verdict":
            item["verdict"] = "NOT_A_VERDICT"
        else:
            del item["reason"]
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "failed" and result.findings[0].assessment is None
    assert result.findings[0].error and len(result.findings[0].evidence) == 1


@pytest.mark.parametrize("field,value", [
    ("reason", None), ("reason", " "), ("reason", {}),
    ("confidence", -1), ("confidence", 2), ("confidence", {}),
])
def test_insufficient_canonicalization_cannot_fill_invalid_schema_fields(field, value):
    calls = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        calls.append(state)
        item = assessment(state["claim_states"]["c0"], sufficient=True) | {"evidence_sufficient": False, field: value}
        return json.dumps([{"claim_id": "c0", "assessment": item}])

    async def search(session, *args):
        await read(session)
        return session.result().model_dump_json()

    result = run(model, search)
    assert len(calls) == 2 and result.status == "failed"
    assert result.findings[0].assessment is None and result.findings[0].error
    assert len(result.findings[0].evidence) == 1
