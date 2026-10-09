"""ROUTE 子图的数值比对、实测来源和判定边界。"""

import asyncio
import json
import pytest
from datetime import datetime, timezone

from backend.extraction.models import Claim
from backend.verification.capabilities import Coordinates, RouteResult, VerificationCapabilities
from backend.verification.models import PlaceReference, VerificationContext
from backend.verification.subgraphs.route import build_route_subgraph
from backend.verification.subgraphs.route.model import RoutePlan
from backend.verification.subgraphs.route.graph import has_claimed_duration

CHECKED = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
EVIDENCE_ID = "c0-r1-0"  # 测量会话生成的首个实测证据标识


class FakeRouting:
    """地图能力替身；返回值由测试指定，不猜测、不折算。"""

    def __init__(self, duration=1080.0, distance=1500.0, unreachable=False):
        self.duration, self.distance, self.unreachable = duration, distance, unreachable

    async def route(self, origin: Coordinates, destination: Coordinates, costing):
        return RouteResult(costing=costing,
                           duration_seconds=None if self.unreachable else self.duration,
                           distance_meters=None if self.unreachable else self.distance)

    async def matrix(self, sources, targets, costing):
        raise AssertionError("ROUTE 单点测量不应调用矩阵")

    async def isochrone(self, origin, costing, minutes):
        raise AssertionError("ROUTE 单点测量不应调用等时圈")


def make_plan(claim_id, claimed=300.0, tolerance=120.0):
    return RoutePlan(claim_id=claim_id, target="示例公园", time_scope="2026-10-07，Asia/Shanghai",
                     origin_text="地铁 2 号线示例站", destination_text="示例公园",
                     transport_mode="pedestrian", claimed_seconds=claimed, tolerance_seconds=tolerance,
                     questions=["从地铁站步行到公园入口需要多久？"])


def assessment(plan, verdict, measured, *, cited=EVIDENCE_ID, claimed=None):
    """判定回显主张数值；cited 用于测试引用不存在证据的场景。"""
    support = [cited] if verdict == "MATCHED" and cited else []
    counter = [cited] if verdict == "MISMATCHED" and cited else []
    return {"target": plan.target, "time_scope": plan.time_scope,
            "verdict": verdict, "confidence": 0.9 if measured is not None else None,
            "evidence_sufficient": measured is not None,
            "reason": "实测明显偏离主张。" if verdict == "MISMATCHED" else "实测与主张一致。",
            "claimed_seconds": plan.claimed_seconds if claimed is None else claimed,
            "transport_mode": plan.transport_mode, "tolerance_seconds": plan.tolerance_seconds,
            "measured_seconds": measured,
            "distance_meters": 1500.0 if measured is not None else None,
            "conditions": [], "supporting_evidence": support, "counter_evidence": counter,
            "context_evidence": [cited] if cited else []}


def inputs(claim_text="从地铁步行 5 分钟即到。"):
    return {"claims": [Claim(claim_id="c0", type="ROUTE", content=claim_text,
                             sources=[{"source_type": "TEXT", "source_ref": None, "source_text": claim_text}])],
            "context": VerificationContext(target_place="示例公园", checked_at=CHECKED)}


def resolve_ok(name="示例公园"):
    async def resolver(text):
        return PlaceReference(name=name, latitude=31.146, longitude=121.656, source="nominatim")
    return resolver


def run(model, routing, resolver=resolve_ok()):
    return asyncio.run(build_route_subgraph().ainvoke(inputs(), context=VerificationCapabilities(
        llm=model, search=None, map_routing=routing, place_resolver=resolver,
        subgraph_timeout_seconds=10)))["result"]


def model_returning(plan, verdict, measured, **kw):
    async def model(prompt, task):
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json")])
        return json.dumps([{"claim_id": plan.claim_id, "assessment": assessment(plan, verdict, measured, **kw)}])
    return model


def test_claim_far_from_measurement_is_a_conflict_showing_both_numbers():
    plan = make_plan("c0", claimed=300.0, tolerance=120.0)
    result = run(model_returning(plan, "MISMATCHED", 1080.0), FakeRouting(duration=1080.0))

    assert result.status == "completed"
    verdict = result.findings[0].assessment
    assert verdict.verdict == "MISMATCHED"
    assert (verdict.claimed_seconds, verdict.measured_seconds) == (300.0, 1080.0)
    assert (verdict.distance_meters, verdict.transport_mode) == (1500.0, "pedestrian")


def test_measurement_inside_tolerance_agrees_with_the_claim():
    plan = make_plan("c0", claimed=300.0, tolerance=120.0)
    result = run(model_returning(plan, "MATCHED", 360.0), FakeRouting(duration=360.0))

    assert result.findings[0].assessment.verdict == "MATCHED"


def test_unresolved_place_is_unverified_and_never_a_fabricated_duration():
    plan = make_plan("c0")

    async def unresolvable(text):
        return None

    result = run(model_returning(plan, "UNVERIFIED", None, cited=None), FakeRouting(), resolver=unresolvable)

    assert result.status == "completed"
    verdict = result.findings[0].assessment
    assert verdict.verdict == "UNVERIFIED"
    assert verdict.measured_seconds is None
    assert any("未解析到具体 POI" in note for note in result.notes)


def test_model_cannot_rewrite_the_claimed_number():
    """结论必须比对用户写下的主张，而不是模型改写后的数字。"""
    plan = make_plan("c0", claimed=300.0)
    result = run(model_returning(plan, "MATCHED", 880.0, claimed=900.0), FakeRouting(duration=880.0))

    finding = result.findings[0]
    assert finding.assessment is None
    assert "改写" in finding.error


def test_verdict_that_contradicts_the_numbers_is_rejected():
    """实测落在容差内却判「数值冲突」，或超出容差却判「数值吻合」，都必须被拒。"""
    plan = make_plan("c0", claimed=300.0, tolerance=120.0)

    within_but_refuted = run(model_returning(plan, "MISMATCHED", 360.0), FakeRouting(duration=360.0))
    assert within_but_refuted.findings[0].assessment is None
    assert "必须为 MATCHED" in within_but_refuted.findings[0].error

    outside_but_agreed = run(model_returning(plan, "MATCHED", 1080.0), FakeRouting(duration=1080.0))
    assert outside_but_agreed.findings[0].assessment is None
    assert "必须为 MISMATCHED" in outside_but_agreed.findings[0].error


def test_measurement_not_recorded_by_the_map_is_rejected():
    """模型不得填一个证据里不存在的实测值——那是用户会读到的数字。"""
    plan = make_plan("c0", claimed=300.0, tolerance=120.0)
    result = run(model_returning(plan, "MISMATCHED", 350.0), FakeRouting(duration=3600.0))

    finding = result.findings[0]
    assert finding.assessment is None
    assert "不在已取得的测量中" in finding.error


def test_citing_evidence_that_was_never_collected_is_rejected():
    plan = make_plan("c0", claimed=300.0, tolerance=120.0)
    result = run(model_returning(plan, "MATCHED", 360.0, cited="invented-id"), FakeRouting(duration=360.0))

    finding = result.findings[0]
    assert finding.assessment is None
    assert "未取得的证据" in finding.error


def test_unreachable_route_is_a_measurement_not_an_error():
    plan = make_plan("c0")
    result = run(model_returning(plan, "UNVERIFIED", None, cited=None), FakeRouting(unreachable=True))

    assert result.status == "completed"
    assert result.findings[0].assessment.measured_seconds is None
    assert any("不可达" in item.content for item in result.findings[0].evidence)


@pytest.mark.parametrize("text", ["断桥 → 白堤 → 苏堤 → 雷峰塔是一条经典步行路线。", "断桥适合作为起点。", "沿湖走 5 公里。"])
def test_route_without_claimed_time_never_invents_a_measurement_plan(text):
    async def unexpected(*args):
        pytest.fail("游览顺序没有时长数值，不能生成时长比对计划")

    result = asyncio.run(build_route_subgraph().ainvoke(inputs(text), context=VerificationCapabilities(
        llm=unexpected, map_routing=FakeRouting(), subgraph_timeout_seconds=10)))["result"]
    assert result.status == "skipped" and not result.selected_claim_ids and not result.findings


@pytest.mark.parametrize("text", ["步行5分钟", "步行约 1.5 小时", "步行半小时", "步行半个小时", "从地铁到公园大约两三分钟", "walking 5 minutes"])
def test_numeric_and_chinese_duration_claims_remain_eligible(text):
    assert has_claimed_duration(inputs(text)["claims"][0])


@pytest.mark.parametrize("text", [
    "手划船票价150元/人，限乘1小时。",
    "摇橹船180元/小时，每次租用1小时起。",
    "游船票价55元/人，游玩1小时。",
    "从码头乘船，1小时150元。",
    "步行沿湖租船，每1小时收费150元。",
    "周末排队二十分钟。",
    "步行入口排队20分钟。",
    "从地铁站出来后排队5分钟。",
    "公园开放12小时。",
    "从9点到17点营业8小时。",
    "在公园步行游玩2小时。",
    "建议沿湖散步游览1小时。",
    "大约两三分钟。",
    "沿湖走5公里，船票价格150元/小时。",
])
def test_non_journey_duration_never_starts_route_planning(text):
    async def unexpected(*args):
        pytest.fail("计费、营业、排队和游玩时长不能作为路线通行耗时")

    result = asyncio.run(build_route_subgraph().ainvoke(inputs(text), context=VerificationCapabilities(
        llm=unexpected, map_routing=FakeRouting(), subgraph_timeout_seconds=10)))['result']
    assert result.status == "skipped"
    assert not result.selected_claim_ids and not result.findings


@pytest.mark.parametrize("text", [
    "从断桥到苏堤大约20分钟。",
    "断桥 → 白堤步行5分钟。",
    "地铁站至公园需10分钟。",
    "走到公园要5分钟，门票50元/人。",
    "步行5分钟到码头，手划船150元/小时。",
    "排队20分钟；从地铁站步行到公园只需5分钟。",
    "公园开放8小时。步行到公园5分钟。",
    "门票50元/人；from station to park takes 5 minutes.",
    "A 5 minute walk from the station to the park.",
    "从地铁站步行5分钟到开放的公园。",
    "骑车10分钟至免费开放的西湖景区。",
    "步行5分钟即可到达开放的码头。",
    "从开放的公园步行5分钟到酒店。",
    "排队20分钟后步行5分钟到公园。",
])
def test_journey_time_is_selected_by_content_even_if_claim_type_is_fact(text):
    claim = inputs(text)['claims'][0].model_copy(update={"type": "FACT"})
    assert has_claimed_duration(claim)


def test_mixed_price_and_journey_claims_only_request_actual_route_plans():
    price = "摇橹船180元/小时，每次租用1小时起。"
    journey = "从地铁站步行到公园5分钟，门票50元/人。"
    state = inputs(journey)
    state['claims'][0] = state['claims'][0].model_copy(update={"type": "FACT"})
    state['claims'].append(Claim(claim_id="c1", type="ROUTE", content=price,
                                 sources=[{"source_type": "TEXT", "source_ref": None,
                                           "source_text": price}]))
    plan = make_plan("c0")
    requested = []

    async def model(prompt, task):
        payload = json.loads(task)
        requested.append(payload['active_claim_ids'])
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json")])
        return json.dumps([{"claim_id": "c0", "assessment": assessment(plan, "MATCHED", 360.0)}])

    result = asyncio.run(build_route_subgraph().ainvoke(state, context=VerificationCapabilities(
        llm=model, map_routing=FakeRouting(duration=360.0), place_resolver=resolve_ok(),
        subgraph_timeout_seconds=10)))['result']
    assert result.status == "completed" and result.selected_claim_ids == ['c0']
    assert requested == [['c0'], ['c0']]
    assert result.findings[0].assessment.verdict == "MATCHED"
