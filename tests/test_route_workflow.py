"""ROUTE 子图的数值比对、实测来源和判定边界。"""

import asyncio
import json
from datetime import datetime, timezone

from backend.extraction.models import Claim
from backend.verification.capabilities import Coordinates, RouteResult, VerificationCapabilities
from backend.verification.models import PlaceReference, VerificationContext
from backend.verification.subgraphs.route import build_route_subgraph
from backend.verification.subgraphs.route.model import RoutePlan

CHECKED = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)


class FakeRouting:
    """地图能力替身；返回值由测试指定，不猜测、不折算。"""

    def __init__(self, duration=1080.0, distance=1500.0, unreachable=False):
        self.duration, self.distance, self.unreachable = duration, distance, unreachable
        self.calls = []

    async def route(self, origin: Coordinates, destination: Coordinates, costing):
        self.calls.append((origin, destination, costing))
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


def assessment(plan, verdict, measured, claimed=None):
    """判定回显主张数值；claimed 用于测试模型试图改写主张数字的场景。"""
    return {"target": plan.target, "time_scope": plan.time_scope,
            "verdict": verdict, "confidence": 0.9 if measured is not None else None,
            "evidence_sufficient": measured is not None,
            "reason": "实测明显偏离主张。" if verdict == "MISMATCHED" else "实测与主张一致。",
            "claimed_seconds": plan.claimed_seconds if claimed is None else claimed,
            "transport_mode": plan.transport_mode, "tolerance_seconds": plan.tolerance_seconds,
            "measured_seconds": measured,
            "distance_meters": 1500.0 if measured is not None else None,
            "conditions": [], "supporting_evidence": ["m0"] if verdict == "MATCHED" else [],
            "counter_evidence": ["m0"] if verdict == "MISMATCHED" else [], "context_evidence": ["m0"]}


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


def model_returning(plan, verdict, measured, claimed=None):
    async def model(prompt, task):
        if prompt.startswith("# Route Plan"):
            return json.dumps([plan.model_dump(mode="json")])
        return json.dumps([{"claim_id": plan.claim_id,
                            "assessment": assessment(plan, verdict, measured, claimed)}])
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

    result = run(model_returning(plan, "UNVERIFIED", None), FakeRouting(), resolver=unresolvable)

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


def test_unreachable_route_is_a_measurement_not_an_error():
    plan = make_plan("c0")
    result = run(model_returning(plan, "UNVERIFIED", None), FakeRouting(unreachable=True))

    assert result.status == "completed"
    assert result.findings[0].assessment.measured_seconds is None
    assert any("不可达" in item.content for item in result.findings[0].evidence)
