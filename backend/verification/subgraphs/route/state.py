"""ROUTE 的全量 State 与节点读写契约；形状与其他类别子图保持一致，便于骨架复用。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from ...models import RouteAssessment
from ...state import SubgraphInput, SubgraphState
from .model import RouteEvidence, RoutePlan


class RouteRoundState(BaseModel):
    """一条主张一轮的测量计数；ROUTE 通常一轮完成，计数仍保留以便诊断。"""

    model_config = ConfigDict(extra="forbid")
    round_number: int = 1
    measurements: int = 0
    search_error: str | None = None


class RouteClaimState(BaseModel):
    """一条选中主张的累积状态。"""

    model_config = ConfigDict(extra="forbid")
    plan: RoutePlan
    evidence: list[RouteEvidence] = Field(default_factory=list)
    rounds: list[RouteRoundState] = Field(default_factory=list)
    assessment: RouteAssessment | None = None
    error: str | None = None

    @model_validator(mode="after")
    def measured_agrees_with_plan(self) -> Self:
        """判定里的主张数值、交通方式和容差必须来自计划，不能由模型改写。

        否则结论比对的是一个被篡改过的主张，而不是用户写下的主张。
        """
        if self.assessment is not None:
            plan, got = self.plan, self.assessment
            if (got.claimed_seconds, got.transport_mode, got.tolerance_seconds) != (
                    plan.claimed_seconds, plan.transport_mode, plan.tolerance_seconds):
                raise ValueError("判定改写了主张的数值、交通方式或容差")
        return self


class RouteState(SubgraphState, total=False):
    """ROUTE 图的通道集合；初始化后进入各节点。"""

    active_claim_ids: list[str]
    claim_states: dict[str, RouteClaimState]
    deadline_at: datetime
    diagnostics: list[str]


class PlanState(SubgraphInput):
    claim_states: dict[str, RouteClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class ValidateState(SubgraphInput):
    claim_states: dict[str, RouteClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
