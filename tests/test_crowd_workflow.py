"""真实 Crowd 图的状态推进、场景对齐边界、判定语义、近似标注与失败收束。

场景条件是消费者能看到的东西：判定里必须留着主张的星期，发布日不符、未知或过期的材料
不能支撑结论，证据不足时只能给「证据不足」，近似来源必须在 notes 里被如实标注。
"""

import asyncio
from datetime import date, datetime, timezone
import json

import pytest

from backend.extraction.models import Claim
from backend.sources.crowd_signal import HONESTY_NOTE, ApproximateCrowdSignal
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationContext
from backend.verification.subgraphs.crowd.graph import build_crowd_subgraph


SCENARIO = "周末"
CLAIM_TEXT = "周末人少，不用排队。"


def make_plan(claim_id):
    return {"claim_id": claim_id, "crowd_type": "QUEUE", "target": "博物馆入口",
            "time_scope": "2026 年 8 月，Asia/Shanghai", "scenario": SCENARIO,
            "questions": ["该入口是否需要排队？"],
            "evidence_strategy": [{"priority": 1, "source_type": "WEB", "purpose": "核对排队情况"}]}


def planned(evidence_ids, **overrides):
    """模型给出的判定骨架；默认把给到的材料全部当作支持证据。"""
    return {"target": "博物馆入口", "time_scope": "2026 年 8 月，Asia/Shanghai", "scenario": SCENARIO,
            "verdict": "SUPPORTED", "confidence": 0.7, "evidence_sufficient": True,
            "reason": "近期材料描述该场景下需要排队。", "conditions": [],
            "supporting_evidence": list(evidence_ids), "counter_evidence": [], "context_evidence": [],
            **overrides}


def unsupported(**overrides):
    return planned([], verdict="UNVERIFIED", confidence=None, evidence_sufficient=False,
                   reason="现有材料未覆盖该场景。", **overrides)


def inputs(count=1, content=CLAIM_TEXT):
    return {"claims": [Claim(claim_id=f"c{i}", type="CROWD", content=content, sources=[
                           {"source_type": "TEXT", "source_ref": None, "source_text": content}])
                       for i in range(count)],
            "context": VerificationContext(target_place="博物馆", checked_at=datetime(2026, 8, 20, tzinfo=timezone.utc))}


def run(model, search=None, sources=None, count=1, timeout=10):
    return asyncio.run(build_crowd_subgraph().ainvoke(inputs(count), context=VerificationCapabilities(
        llm=model, search=search, evidence_sources=sources or {}, subgraph_timeout_seconds=timeout,
    )))["result"]


async def read(session, *, published_at=None, content="周末下午排队约二十分钟。", url="https://example.com/note"):
    async def operation():
        return {"source": "游客游记", "content": content, "url": url,
                "published_at": published_at, "source_type": "WEB"}
    return await session.execute(operation)


def evidence_of(state):
    return state["claim_states"]["c0"]["evidence"]


def two_step(judge, published_at=date(2026, 8, 15), content="周六下午排队约二十分钟。"):
    """返回一对模型/取证替身：先给计划，再把已取材料交给判定替身。"""

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            return json.dumps([make_plan("c0")])
        return json.dumps([{"claim_id": "c0", "assessment": judge(evidence_of(state))}])

    async def search(session, *args):
        await read(session, published_at=published_at, content=content)
        return session.result().model_dump_json()

    return model, search


def test_scenario_and_time_alignment_reach_the_verdict():
    def judged(evidence):
        return planned([item["evidence_id"] for item in evidence], evidence_time_coverage="模型自报的覆盖范围")

    result = run(*two_step(judged))
    assert result.status == "completed"
    assessment = result.findings[0].assessment
    assert assessment.verdict == "SUPPORTED" and assessment.scenario == SCENARIO
    assert assessment.supporting_evidence == [result.findings[0].evidence[0].evidence_id]
    assert assessment.evidence_time_coverage.startswith("场景条件：周末")
    assert "2026-08-15" in assessment.evidence_time_coverage
    assert "模型自报" not in assessment.evidence_time_coverage


@pytest.mark.parametrize("published_at,reason", [
    (date(2026, 8, 11), "场景不符"),  # 周二发布，证明不了周末的拥挤度
    (None, "发布时间未知"),           # 没有发布日的材料不能支撑结论
    (date(2019, 8, 17), "超出时效"),  # 早年的拥挤度描述证明不了现在还成立
])
def test_material_outside_the_claim_scenario_cannot_support_a_verdict(published_at, reason):
    model, search = two_step(lambda evidence: planned([item["evidence_id"] for item in evidence]),
                             published_at=published_at, content="到访时没有排队。")
    result = run(model, search)
    assert result.status == "failed"
    finding = result.findings[0]
    assert finding.assessment is None and reason in finding.error
    assert len(finding.evidence) == 1  # 材料保留，只是不能用来支持结论


@pytest.mark.parametrize("broken,reason", [
    ("SCENARIO_ONLY", "必须说明具体成立条件"),
    ("NOT_SUPPORTED", "必须引用场景对齐的反证"),
])
def test_verdict_semantics_require_their_own_evidence(broken, reason):
    def judged(evidence):
        ids = [item["evidence_id"] for item in evidence]
        if broken == "SCENARIO_ONLY":
            return planned(ids, verdict="SCENARIO_ONLY", conditions=[])
        return planned([], verdict=broken, counter_evidence=[])

    result = run(*two_step(judged))
    assert result.findings[0].assessment is None and reason in result.findings[0].error


def test_scenario_only_conditions_stay_traceable_in_the_verdict():
    def judged(evidence):
        return planned([item["evidence_id"] for item in evidence],
                       verdict="SCENARIO_ONLY", conditions=["只在周末下午成立"])

    result = run(*two_step(judged))
    assessment = result.findings[0].assessment
    assert assessment.verdict == "SCENARIO_ONLY"
    assert assessment.conditions == ["只在周末下午成立"] and assessment.scenario == SCENARIO


def test_insufficient_evidence_yields_unverified_and_discloses_the_approximation():
    async def profile(query):
        assert "博物馆入口" in query and SCENARIO in query
        return [{"title": "游记", "content": "周末下午入场排了很久。", "published_at": "2026-08-15"},
                {"title": "游记", "content": "其他时候不用排队。", "published_at": "2026-08-13"}]

    def judged(evidence):
        # 近似信号只有一条聚合材料，不构成对具体场景的核实。
        return unsupported(context_evidence=[item["evidence_id"] for item in evidence])

    result = run(*two_step(judged), sources={"crowd_signal": ApproximateCrowdSignal(profile)})
    assert result.status == "completed"
    assessment = result.findings[0].assessment
    assert assessment.verdict == "UNVERIFIED" and assessment.confidence is None
    assert assessment.evidence_sufficient is False
    assert any(HONESTY_NOTE in note for note in result.notes)
    signal = next(item for item in result.findings[0].evidence if item.url is None)
    assert "公开评价密度近似" in signal.source and "不是客流或交易实测数据" in signal.content


def test_approximate_signal_counts_recent_crowding_mentions_only():
    async def profile(query):
        return [{"content": "周末排队很久", "published_at": "2026-08-15"},
                {"content": "不用排队", "published_at": "2026-08-16"},
                {"content": "人山人海", "published_at": "2020-01-01"}]

    evidence = asyncio.run(ApproximateCrowdSignal(profile).search("博物馆 周末"))
    assert len(evidence) == 1
    assert "2 条公开评价" in evidence[0].content and "1 条提到拥挤或排队" in evidence[0].content
    assert evidence[0].published_at == date(2026, 8, 16)
    assert asyncio.run(ApproximateCrowdSignal(lambda query: _empty()).search("博物馆 周末")) == []


def test_signal_without_a_review_feed_reports_missing_data():
    # 本次运行只有近似来源、没有网页取证：没有评价数据就没有任何材料。
    model, _ = two_step(lambda evidence: unsupported())
    result = run(model, None, sources={"crowd_signal": ApproximateCrowdSignal()})
    assert result.status == "completed"
    assert result.findings[0].assessment.verdict == "UNVERIFIED" and result.findings[0].evidence == []
    assert any("尚未接入公开评价数据供应商" in note for note in result.notes)


def test_second_round_keeps_the_scenario_and_only_aligned_material_can_support():
    scenarios, prompts = [], []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            scenarios.append(state["claim_states"].get("c0", {}).get("plan", {}).get("scenario"))
            return json.dumps([make_plan("c0")])
        evidence = state["claim_states"]["c0"]["evidence"]
        return json.dumps([{"claim_id": "c0", "assessment": (
            planned([item["evidence_id"] for item in evidence]) if evidence else unsupported())}])

    async def search(session, prompt, task):
        prompts.append(json.loads(task)["claim_state"]["plan"]["scenario"])
        if session.round.round_number == 2:
            await read(session, published_at=date(2026, 8, 15))
        return session.result().model_dump_json()

    result = run(model, search)
    assert scenarios == [None, SCENARIO] and prompts == [SCENARIO, SCENARIO]
    assert result.status == "completed"
    assert result.findings[0].assessment.verdict == "SUPPORTED"
    assert result.findings[0].assessment.scenario == SCENARIO


async def _empty():
    return []


def test_place_names_are_not_mistaken_for_seasonal_conditions():
    """「秋叶原」这类地名含季节字，但不是季节条件；误判会给主张凭空加时间约束。"""

    def judge(evidence):
        return planned([item["evidence_id"] for item in evidence],
                       scenario="秋叶原", conditions=[], reason="六月材料描述该场景排队。")

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            plan = make_plan("c0") | {"scenario": "秋叶原"}
            return json.dumps([plan])
        return json.dumps([{"claim_id": "c0", "assessment": judge(evidence_of(state))}])

    async def search(session, *args):
        await read(session, published_at=date(2026, 6, 13), content="周六下午排队约二十分钟。")
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "completed"
    assert result.findings[0].assessment.verdict == "SUPPORTED"


def test_counter_scenario_questions_do_not_reject_matching_evidence():
    """提示词要求 questions 写反证场景；这些字不能被当成主张自身的条件。"""

    def judge(evidence):
        return planned([item["evidence_id"] for item in evidence],
                       scenario="工作日早上", reason="周一早上的材料描述该场景不需排队。")

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Crowd Plan"):
            plan = make_plan("c0") | {
                "scenario": "工作日早上",
                "questions": ["工作日早上是否需要排队？", "周末前往的游客是否反映排队很久？"],
            }
            return json.dumps([plan])
        return json.dumps([{"claim_id": "c0", "assessment": judge(evidence_of(state))}])

    async def search(session, *args):
        await read(session, published_at=date(2026, 8, 17), content="周一早上入场无需排队。")
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "completed"
    assert result.findings[0].assessment is not None
    assert result.findings[0].assessment.verdict == "SUPPORTED"
