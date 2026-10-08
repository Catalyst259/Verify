"""ROUTE 的实测会话与地图取证：解析起终点并测量路线，不做网页取证。

测量是确定性的——给定起终点和交通方式，地图能力直接返回时长与距离——因此 ROUTE 不需要
browser-use 那样的工具循环，也没有「模型选择动作」这一步。
"""

import json
from datetime import datetime, timezone

from ...capabilities import Coordinates, MapRouting
from ...models import PlaceReference
from .model import MAX_MEASUREMENTS, RouteEvidence, RouteSearchResult
from .state import RouteClaimState, RouteRoundState


class RouteSearchSession:
    """一条 ROUTE 主张一轮的实测记录；地图调用失败也保留已取得的测量。"""

    def __init__(self, claim: RouteClaimState, deadline_at: datetime, *, checked_at: datetime | None = None,
                 input_urls: tuple[str, ...] = (), evidence_sources=None):
        self.claim_id = claim.plan.claim_id
        self.plan = claim.plan
        self.evidence: list[RouteEvidence] = []
        self.round = RouteRoundState(round_number=len(claim.rounds) + 1)
        self.diagnostics: list[str] = []
        self.checked_at = checked_at or datetime.now(timezone.utc)
        self.deadline_at = deadline_at

    @property
    def diagnostic_context(self) -> dict:
        return {"claim_id": self.claim_id, "round_number": self.round.round_number,
                "transport_mode": self.plan.transport_mode}

    def remaining_budget(self) -> dict:
        return {"measurements": MAX_MEASUREMENTS - len(self.evidence)}

    def exhausted(self) -> bool:
        return len(self.evidence) >= MAX_MEASUREMENTS

    def update_round(self, **changes):
        self.round = RouteRoundState.model_validate(self.round.model_dump() | changes)

    def add(self, item: RouteEvidence):
        if len(self.evidence) < MAX_MEASUREMENTS:
            self.evidence.append(item)

    def result(self, raw: str | None = None) -> RouteSearchResult:
        """以实际测量记录为准；调用方输出不覆盖已取得的测量。"""
        return RouteSearchResult(claim_id=self.claim_id, evidence=self.evidence, error=self.round.search_error)


def _coordinates(place: PlaceReference) -> Coordinates | None:
    if place is None or place.latitude is None or place.longitude is None:
        return None
    return Coordinates(latitude=place.latitude, longitude=place.longitude)


async def run_route_search(session: RouteSearchSession, system_prompt: str, task: str, *,
                           routing: MapRouting, resolver) -> str:
    """按计划解析起终点并实测一条路线。

    解析不到具体 POI 或路线不可达都不是异常：前者只能判证据不足，后者是一条真实测量
    （measured_seconds 为 None）。两者都不得用直线距离折算出一个时长来充数。
    """
    plan = session.plan
    origin = await resolver(plan.origin_text) if resolver is not None else None
    destination = await resolver(plan.destination_text) if resolver is not None else None
    start, end = _coordinates(origin), _coordinates(destination)
    if start is None or end is None:
        session.update_round(search_error="起终点未解析到具体 POI，无法测量路线")
        return json.dumps({"claim_id": session.claim_id, "evidence": [], "error": session.round.search_error})

    result = await routing.route(start, end, plan.transport_mode)
    session.add(RouteEvidence(
        evidence_id=f"{session.claim_id}-r{session.round.round_number}-{len(session.evidence)}",
        origin_name=origin.name, destination_name=destination.name, transport_mode=result.costing,
        measured_seconds=result.duration_seconds, distance_meters=result.distance_meters,
        source="map_routing",
        content=(f"{result.costing} 实测 {result.duration_seconds:.0f} 秒 / {result.distance_meters:.0f} 米"
                 if result.duration_seconds is not None and result.distance_meters is not None
                 else f"{result.costing} 实测：该组合不可达或供应商未给出结果"),
        retrieved_at=datetime.now(timezone.utc),
    ))
    session.update_round(measurements=len(session.evidence))
    return json.dumps({"claim_id": session.claim_id, "evidence": [], "error": None})
