"""Fact 节点契约、跨轮引用与公共响应序列化的业务约束。"""

import json

from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
import pytest

from backend import main
from backend.extraction.models import ClaimExtractionResult
from backend.verification.models import ClaimFinding, Evidence, FactAssessment, SubgraphResult
from backend.verification.state import SubgraphInput, SubgraphOutput, SubgraphState
from backend.verification.subgraphs import build_placeholder_subgraph
from backend.verification.subgraphs.facts.model import FactEvidence, FactPlan, SearchResult, ValidateResult
from backend.verification.subgraphs.facts.state import FactClaimState, FactRoundState


@pytest.fixture
def plan():
    return FactPlan.model_validate_json(json.dumps({
        "claim_id": "claim_001", "fact_type": "PRICE_POLICY", "target": "示例公园",
        "time_scope": "2026-10-01 至 2026-10-07，Asia/Shanghai",
        "questions": ["国庆期间是否免费进入？", "是否有收费区域或人群限制？"],
        "evidence_strategy": [{"priority": 1, "source_type": "OFFICIAL", "purpose": "确认国庆政策与例外"}],
    }))


@pytest.fixture
def evidence():
    return FactEvidence.model_validate_json(json.dumps({
        "evidence_id": "e1", "source": "示例公园日常游园说明", "source_type": "OFFICIAL",
        "url": "https://example.org/park/notice", "published_at": "2026-08-20",
        "retrieved_at": "2026-10-05T08:00:00Z",
        "content": "公共区域日常免费开放，节假日安排以另行公告为准。",
    }))


@pytest.fixture
def assessment(plan):
    return FactAssessment.model_validate_json(json.dumps({
        "target": plan.target, "time_scope": plan.time_scope, "verdict": "UNVERIFIED", "confidence": None,
        "evidence_sufficient": False, "reason": "e1 未说明目标节假日的政策。", "conditions": [],
        "supporting_evidence": [], "counter_evidence": [], "context_evidence": ["e1"],
        "dimensions": {"authority": 0.9, "directness": 0.4, "recency": 0.6, "context_match": 0.4, "independence": None},
        "remaining_gaps": [{"question": "国庆是否免费？", "preferred_source": "OFFICIAL", "reason": "未覆盖国庆。"}],
    }))


def test_node_json_contracts_preserve_source_date_and_unknown_publication(plan, evidence, assessment):
    search = SearchResult.model_validate_json(json.dumps({
        "claim_id": plan.claim_id, "evidence": [evidence.model_dump(mode="json")], "error": None,
    }))
    validated = ValidateResult.model_validate_json(json.dumps({
        "claim_id": plan.claim_id, "assessment": assessment.model_dump(mode="json"),
    }))
    assert search.evidence[0].model_dump(mode="json")["published_at"] == "2026-08-20"
    assert validated.assessment == assessment
    without_date = evidence.model_dump(exclude={"published_at"})
    assert FactEvidence.model_validate(without_date).published_at is None
    with_time = FactEvidence.model_validate_json(json.dumps(evidence.model_dump(mode="json") | {
        "published_at": "2026-08-20T00:00:00Z",
    }))
    assert with_time.model_dump(mode="json")["published_at"] == "2026-08-20T00:00:00Z"
    # 旧来源可继续使用原有三个字段。
    assert Evidence(source="旧来源", content="材料", url=None).evidence_id is None


@pytest.mark.parametrize("change", [
    {"evidence_id": None}, {"source_type": "UNKNOWN"}, {"url": "file:///notice"},
    {"retrieved_at": "2026-10-05T08:00:00"}, {"content": " \n\t"},
])
def test_fact_web_evidence_requires_traceable_material(evidence, change):
    with pytest.raises(ValidationError):
        FactEvidence.model_validate(evidence.model_dump() | change)


@pytest.mark.parametrize("change", [
    {"fact_type": "UNKNOWN"}, {"questions": []}, {"target": " "}, {"unexpected": True},
])
def test_plan_rejects_invalid_structure(plan, change):
    with pytest.raises(ValidationError):
        FactPlan.model_validate(plan.model_dump() | change)


@pytest.mark.parametrize("change", [
    {"confidence": 0.8}, {"verdict": "SUPPORTED", "supporting_evidence": ["e1"]},
    {"evidence_sufficient": True, "verdict": "SUPPORTED"},
    {"evidence_sufficient": True, "verdict": "CONTRADICTED"},
    {"evidence_sufficient": True, "verdict": "CONDITIONAL", "supporting_evidence": ["e1"]},
    {"evidence_sufficient": True, "verdict": "CONDITIONAL", "conditions": ["仅公共区域"]},
])
def test_verdict_requires_sufficient_and_corresponding_evidence(assessment, change):
    with pytest.raises(ValidationError):
        FactAssessment.model_validate(assessment.model_dump() | change)


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf")])
def test_dimension_scores_reject_out_of_range_values(assessment, score):
    payload = assessment.model_dump()
    payload["dimensions"]["authority"] = score
    with pytest.raises(ValidationError):
        FactAssessment.model_validate(payload)


@pytest.mark.parametrize("change", [
    {"round_number": 3}, {"tool_calls": 21}, {"tool_calls": 1, "queries": 2, "results_per_query": [0, 0]},
    {"tool_calls": 6, "queries": 6, "results_per_query": [0] * 6},
    {"tool_calls": 1, "queries": 1, "results_per_query": [11]},
    {"tool_calls": 1, "queries": 1}, {"new_evidence_count": 16}, {"tool_calls": -1},
])
def test_per_claim_round_budget_is_bounded(change):
    with pytest.raises(ValidationError):
        FactRoundState.model_validate({"round_number": 1} | change)


def test_search_failure_preserves_material_and_empty_search_is_not_failure(plan, evidence):
    empty = SearchResult(claim_id=plan.claim_id, evidence=[], error=None)
    partial = SearchResult(claim_id=plan.claim_id, evidence=[evidence], error="第二个页面读取失败")
    assert empty.error is None
    assert partial.evidence == [evidence]
    with pytest.raises(ValidationError):
        SearchResult(claim_id=plan.claim_id, evidence=[evidence] * 16, error=None)


def test_second_round_preserves_first_round_references_and_changed_page(plan, evidence, assessment):
    initial = FactClaimState(plan=plan, evidence=[evidence], assessment=assessment, rounds=[
        FactRoundState(round_number=1, tool_calls=2, queries=1, results_per_query=[1], new_evidence_count=1),
    ])
    new_evidence = FactEvidence.model_validate(evidence.model_dump() | {
        "evidence_id": "e2", "content": "国庆期间公共区域免费开放，古典园林区域另购票。",
    })
    conditional = FactAssessment.model_validate(assessment.model_dump() | {
        "verdict": "CONDITIONAL", "confidence": 0.9, "evidence_sufficient": True,
        "reason": "e2 确认公共区域免费，古典园林收费。", "conditions": ["仅公共区域免费"],
        "supporting_evidence": ["e2"], "remaining_gaps": [],
    })
    updated = FactClaimState.model_validate(initial.model_dump() | {
        "evidence": [evidence, new_evidence], "assessment": conditional,
        "rounds": [*initial.rounds, FactRoundState(round_number=2, tool_calls=1, new_evidence_count=1)],
    })
    assert updated.assessment.context_evidence == ["e1"]
    assert [item.evidence_id for item in updated.evidence] == ["e1", "e2"]
    assert updated.evidence[0].url == updated.evidence[1].url
    assert initial.assessment.verdict == "UNVERIFIED"
    with pytest.raises(ValidationError):
        FactClaimState.model_validate(updated.model_dump() | {"evidence": [new_evidence]})


@pytest.mark.parametrize("problem", ["unknown_reference", "different_scope", "duplicate_id", "missing_first_round"])
def test_claim_state_rejects_broken_history_and_references(plan, evidence, assessment, problem):
    payload = {"plan": plan.model_dump(), "evidence": [evidence.model_dump()], "assessment": assessment.model_dump()}
    if problem == "unknown_reference":
        payload["assessment"]["context_evidence"] = ["e2"]
    elif problem == "different_scope":
        payload["assessment"]["time_scope"] = "日常开放"
    elif problem == "duplicate_id":
        payload["evidence"].append(evidence.model_dump())
    else:
        payload["rounds"] = [{"round_number": 2}]
    with pytest.raises(ValidationError):
        FactClaimState.model_validate(payload)


@pytest.mark.parametrize("status,error", [
    ("completed", None), ("partial", "补搜超时，保留首轮有效判定"),
])
def test_fact_assessment_and_metadata_survive_public_response(tmp_path, plan, evidence, assessment, status, error):
    result = SubgraphResult(
        graph_name="fact", status=status, selected_claim_ids=[plan.claim_id],
        findings=[ClaimFinding(
            claim_id=plan.claim_id, summary=assessment.reason, evidence=[evidence], assessment=assessment,
            error=error,
        )],
    )

    async def extract(*args):
        return ClaimExtractionResult(target_place=plan.target, claims=[{
            "claim_id": plan.claim_id, "type": "FACT", "content": "国庆免费进入。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "国庆免费进入。"}],
        }])

    def finalize(state: SubgraphInput) -> SubgraphOutput:
        return {"result": result}

    builder = StateGraph(SubgraphState, input_schema=SubgraphInput, output_schema=SubgraphOutput)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "finalize")
    builder.add_edge("finalize", END)
    with TestClient(main.create_app(tmp_path, extract, subgraphs={"fact": builder.compile()})) as client:
        response = client.post("/api/verifications", json={"target_place": plan.target, "text": "国庆免费进入。"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == status
    finding = payload["subgraph_results"]["fact"]["findings"][0]
    assert finding["assessment"] == assessment.model_dump(mode="json")
    assert finding["evidence"][0] == evidence.model_dump(mode="json")
    assert finding["error"] == error


def test_subgraph_schemas_expose_only_input_and_final_result():
    graph = build_placeholder_subgraph("fact")
    assert set(graph.get_input_jsonschema()["properties"]) == {"claims", "context"}
    assert set(graph.get_output_jsonschema()["properties"]) == {"result"}
