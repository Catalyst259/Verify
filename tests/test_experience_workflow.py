"""真实 Experience 图的体验一致度边界：场景分化、转载重复与证据不足。

判定语义是「来源之间是否一致」而不是真假，因此这里只断言消费者能看到的
结果：结论落在体验词表内、场景分化不被并成一致、同文转载不构成第二个
来源、证据不足时不产出任何确定结论。
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.extraction.models import Claim
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationContext
from backend.verification.subgraphs.experience.graph import build_experience_subgraph


def make_plan(claim_id):
    return {"claim_id": claim_id, "experience_type": "COMFORT", "target": "民宿客房隔音",
            "time_scope": "2026 年 9 月，Asia/Shanghai",
            "dimensions": ["客房内噪音", "墙体与门隔音"],
            "questions": ["住客是否反映夜间能听到走廊或街道噪音？"],
            "evidence_strategy": [{"priority": 1, "source_type": "WEB", "purpose": "寻找近期住客自述"}]}


def judgment(plan, **changes):
    return {"target": plan["target"], "time_scope": plan["time_scope"],
            "verdict": "UNVERIFIED", "confidence": None, "evidence_sufficient": False,
            "reason": "尚无独立体验材料。", "conditions": [], "supporting_evidence": [],
            "counter_evidence": [], "context_evidence": [], "source_agreement": None} | changes


def inputs(count=1):
    return {"claims": [Claim(claim_id=f"c{i}", type="EXPERIENCE", content="民宿隔音很好。", sources=[
                {"source_type": "TEXT", "source_ref": None, "source_text": "民宿隔音很好。"}])
            for i in range(count)],
            "context": VerificationContext(target_place="民宿", checked_at=datetime.now(timezone.utc))}


def run(model, search=None, count=1):
    return asyncio.run(build_experience_subgraph().ainvoke(inputs(count), context=VerificationCapabilities(
        llm=model, search=search, subgraph_timeout_seconds=10)))["result"]


async def read(session, source, content):
    async def operation():
        return {"source": source, "content": content, "url": f"https://example.com/{source}",
                "source_type": "WEB"}
    return await session.execute(operation)


def test_scenario_dependent_survives_with_its_conditions_and_opposing_sources():
    """同一主张在不同房型下体验相反：结论必须是场景依赖，并保留两方来源。"""
    seen = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Experience Plan"):
            assert "输出 JSON Schema" in prompt
            return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
        assert prompt.startswith("# Experience Validate")
        claim = state["claim_states"]["c0"]
        seen.append(claim["plan"]["dimensions"])
        ids = [item["evidence_id"] for item in claim["evidence"]]
        return json.dumps([{"claim_id": "c0", "assessment": judgment(
            claim["plan"], verdict="SCENARIO_DEPENDENT", confidence=0.6, evidence_sufficient=True,
            reason="两位住客的独立自述随房型变化，不能概括为整体一致。", source_agreement=0.5,
            conditions=["临街大床房与高层庭院房的隔音体验不同"],
            supporting_evidence=[ids[0]], counter_evidence=[ids[1]])}])

    async def search(session, prompt, task):
        assert prompt.startswith("# Experience Search")
        await read(session, "住客甲", "临街大床房夜间能听到车流噪音，几乎每晚都被吵醒。")
        await read(session, "住客乙", "高层庭院房很安静，住了三晚没有被走廊声音打扰。")
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.status == "completed"
    finding = result.findings[0]
    assert finding.assessment.verdict == "SCENARIO_DEPENDENT"
    assert finding.assessment.conditions == ["临街大床房与高层庭院房的隔音体验不同"]
    assert len({item.evidence_id for item in finding.evidence}) == 2
    assert len(finding.assessment.supporting_evidence + finding.assessment.counter_evidence) == 2
    assert seen == [["客房内噪音", "墙体与门隔音"]]


def test_identical_reposts_do_not_count_as_a_second_source():
    """两个平台同文转载不能支撑「体验较一致」，两份不同体验才可以。"""

    def model_with(verdict):
        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Experience Plan"):
                return json.dumps([make_plan("c0")])
            claim = state["claim_states"]["c0"]
            ids = [item["evidence_id"] for item in claim["evidence"]]
            return json.dumps([{"claim_id": "c0", "assessment": judgment(
                claim["plan"], verdict=verdict, confidence=0.7, evidence_sufficient=True,
                reason="两个来源说法一致。", source_agreement=0.8, supporting_evidence=ids)}])
        return model

    async def repost_search(session, prompt, task):
        body = "这家民宿隔音很好，晚上完全听不到走廊的声音。"
        await read(session, "平台甲", body)
        await read(session, "平台乙", body)
        return session.result().model_dump_json()

    async def independent_search(session, prompt, task):
        await read(session, "住客甲", "住了两晚都很安静，走廊没有声音。")
        await read(session, "住客乙", "隔音不错，隔壁说话听不到。")
        return session.result().model_dump_json()

    reposted = run(model_with("CONSISTENT"), repost_search)
    assert reposted.status == "failed"
    assert reposted.findings[0].assessment is None and "两份内容不同" in reposted.findings[0].error
    # 材料仍然交给用户，只是不能据此宣称来源一致。
    assert len(reposted.findings[0].evidence) == 2

    independent = run(model_with("CONSISTENT"), independent_search)
    assert independent.status == "completed"
    assert independent.findings[0].assessment.verdict == "CONSISTENT"


@pytest.mark.parametrize("verdict,sufficient,problem", [
    ("CONSISTENT", True, "两份内容不同"),
    ("UNVERIFIED", True, "证据不足"),
])
def test_single_source_cannot_produce_a_definite_verdict(verdict, sufficient, problem):
    async def model(prompt, task):
        if prompt.startswith("# Experience Plan"):
            return json.dumps([make_plan("c0")])
        claim = json.loads(task)["claim_states"]["c0"]
        ids = [item["evidence_id"] for item in claim["evidence"]]
        return json.dumps([{"claim_id": "c0", "assessment": judgment(
            claim["plan"], verdict=verdict, confidence=0.9, evidence_sufficient=sufficient,
            reason="只有一篇笔记。", source_agreement=0.9, supporting_evidence=ids)}])

    async def search(session, prompt, task):
        await read(session, "住客甲", "民宿隔音很好，晚上很安静。")
        return session.result().model_dump_json()

    result = run(model, search)
    assert result.findings[0].assessment is None
    assert problem in result.findings[0].error


def test_unverified_triggers_one_more_round_then_reports_agreement():
    """首轮只有单一来源时不得给结论，补搜到第二份独立材料后才能判为一致。"""
    rounds = []

    async def model(prompt, task):
        state = json.loads(task)
        if prompt.startswith("# Experience Plan"):
            return json.dumps([make_plan("c0")])
        claim = state["claim_states"]["c0"]
        ids = [item["evidence_id"] for item in claim["evidence"]]
        rounds.append(len(claim["evidence"]))
        if len(ids) < 2:
            return json.dumps([{"claim_id": "c0", "assessment": judgment(claim["plan"])}])
        return json.dumps([{"claim_id": "c0", "assessment": judgment(
            claim["plan"], verdict="CONSISTENT", confidence=0.7, evidence_sufficient=True,
            reason="两份独立自述都认为安静。", source_agreement=0.9, supporting_evidence=ids)}])

    async def search(session, prompt, task):
        number = json.loads(task)["round_number"]
        await read(session, f"住客{number}", f"第 {number} 位住客：入住期间没有听到噪音。")
        return session.result().model_dump_json()

    result = run(model, search)
    assert rounds == [1, 2]
    assert result.status == "completed"
    assert result.findings[0].assessment.verdict == "CONSISTENT"
    assert len(result.findings[0].evidence) == 2


def test_plan_without_experience_claims_skips_and_broken_plan_fails():
    async def model(prompt, task):
        return "[]" if prompt.startswith("# Experience Plan") else pytest.fail("不应进入判定")

    result = run(model, count=0)
    assert result.status == "skipped" and result.error is None

    async def broken(prompt, task):
        return "invalid"

    failed = run(broken)
    assert failed.status == "failed" and "Plan" in failed.error
