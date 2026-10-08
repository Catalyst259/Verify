"""Experience 的全量 State 与逐 Claim 执行约束。

体验核验与 Fact 共用同一套网页取证会话和相同的轮次、工具预算基线，
所以轮次计数、证据唯一性、判定范围回显等不变量直接继承 Fact 的状态
模型，只把计划与判定换成体验语义的两个类型。
"""

from datetime import datetime

from ...models import ExperienceAssessment
from ...state import SubgraphInput, SubgraphState
from ..facts.state import FactClaimState
from .model import ExperiencePlan


class ExperienceClaimState(FactClaimState):
    """一条选中 Claim 的累积状态；补搜保留已有证据、判定和轮次记录。

    plan 保存当前计划，补搜不能改变原始主张与目标范围。assessment 只在
    来源足以形成一致度判断时存在；没有任何独立体验材料时必须留空，由
    Validate 给出 UNVERIFIED，不能凭主观印象造出结论。
    """

    plan: ExperiencePlan
    assessment: ExperienceAssessment | None = None


class ExperienceState(SubgraphState, total=False):
    """Experience 图的通道集合；初始化后再进入各节点的必填 State。

    claim_states 按 claim_id 索引，键必须等于 plan.claim_id，并属于原始
    claims；选中后与 selected_claim_ids 一一对应。active_claim_ids 只包含
    当前轮待处理的主张，不能删除已完成或失败的 claim_states。
    """

    claim_states: dict[str, ExperienceClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
    # 执行诊断仅供日志与最终 notes，不属于 PlanState/ValidateState 的模型输入。
    diagnostics: list[str]


class PlanState(SubgraphInput):
    """Plan 读取原始主张、上下文和历史状态。

    首轮 claim_states 为空，active_claim_ids 为所有候选主张；Plan 自行
    选择主观体验内容，跳过能用官方或产品数据证伪的事实状态。
    """

    claim_states: dict[str, ExperienceClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class SearchState(SubgraphInput):
    """活动主张必须已有计划；Search 只对这些主张执行本轮取证。"""

    claim_states: dict[str, ExperienceClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class ValidateState(SubgraphInput):
    """Validate 读取活动主张的计划与所有轮次累积的体验材料。"""

    claim_states: dict[str, ExperienceClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
