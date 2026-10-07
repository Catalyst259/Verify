"""CROWD 的全量 State 与逐 Claim 执行约束。

客流核验与 Fact 共用同一套网页取证会话和相同的轮次、工具预算基线，因此轮次计数、
证据唯一性、判定范围回显等不变量直接继承 Fact 的状态模型，只把计划、证据与判定
换成客流语义的类型。
"""

from datetime import datetime

from pydantic import Field

from ...models import CrowdAssessment, Evidence
from ...state import SubgraphInput, SubgraphState
from ..facts.model import MAX_EVIDENCE_PER_ROUND, MAX_ROUNDS
from ..facts.state import FactClaimState
from .model import CrowdPlan


class CrowdClaimState(FactClaimState):
    """一条选中 Claim 的累积状态；补搜保留已有证据、判定和轮次记录。

    plan 保存当前计划，补搜不能改变原主张的目标范围与场景条件；改变场景
    等于核验另一件事。assessment 只在场景对齐的材料足够时存在，没有材料时
    由 Validate 给出 UNVERIFIED，不能凭常识补出客流结论。
    """

    plan: CrowdPlan
    assessment: CrowdAssessment | None = None
    # 客流近似信号不是网页、没有 URL，不能沿用必填 URL 的网页证据模型；
    # 证据唯一、轮次连续与判定范围回显等校验仍由父类继续生效。
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_ROUNDS * MAX_EVIDENCE_PER_ROUND)


class CrowdState(SubgraphState, total=False):
    """CROWD 图的通道集合；初始化后再进入各节点的必填 State。"""

    claim_states: dict[str, CrowdClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
    # 执行诊断仅供日志与最终 notes，不属于 PlanState/ValidateState 的模型输入。
    diagnostics: list[str]


class PlanState(SubgraphInput):
    """Plan 读取原始主张、上下文和历史状态。

    首轮 claim_states 为空，active_claim_ids 为所有候选主张；Plan 自行
    选择客流拥挤度类内容，跳过与拥挤度无关的主张。
    """

    claim_states: dict[str, CrowdClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class SearchState(SubgraphInput):
    """活动主张必须已有计划；Search 只对这些主张执行本轮取证。"""

    claim_states: dict[str, CrowdClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class ValidateState(SubgraphInput):
    """Validate 读取活动主张的计划、所有轮次累积的材料与代码算出的场景对齐。"""

    claim_states: dict[str, CrowdClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
