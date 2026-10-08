"""Fact 的全量 State、逐 Claim 执行约束与节点读写契约。

TypedDict 描述 LangGraph 通道和各阶段必需的字段，不提供运行时校验。
节点边界使用 Pydantic 模型校验 JSON，并以完整的新 FactClaimState 写回，
避免原地修改绕过校验。模型、搜索及页面读取能力始终由 runtime 注入。
"""

from datetime import datetime
from typing import Annotated, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...models import FactAssessment, NonEmptyText
from ...state import SubgraphInput, SubgraphOutput, SubgraphState
from .model import (
    MAX_EVIDENCE_PER_ROUND,
    MAX_QUERIES,
    MAX_RESULTS_PER_QUERY,
    MAX_ROUNDS,
    MAX_TOOL_CALLS,
    FactEvidence,
    FactPlan,
)


class FactRoundState(BaseModel):
    """一条 Claim 一轮的取证计数，由代码在每次调用前检查并记录。

    tool_calls 包含搜索、读取及失败重试，不包含 done；查询也消耗工具
    调用额度。results_per_query 记录每次查询的候选数，失败查询记为 0。
    new_evidence_count 是去重后本轮实际新增的材料数。search_error 仅记录
    该轮可恢复的取证错误，不直接使最终 Finding 失败。
    """

    model_config = ConfigDict(extra="forbid")
    round_number: Annotated[int, Field(strict=True, ge=1, le=MAX_ROUNDS)]
    tool_calls: Annotated[int, Field(strict=True, ge=0, le=MAX_TOOL_CALLS)] = 0
    queries: Annotated[int, Field(strict=True, ge=0, le=MAX_QUERIES)] = 0
    results_per_query: list[Annotated[int, Field(strict=True, ge=0, le=MAX_RESULTS_PER_QUERY)]] = Field(
        default_factory=list, max_length=MAX_QUERIES,
    )
    new_evidence_count: Annotated[int, Field(strict=True, ge=0, le=MAX_EVIDENCE_PER_ROUND)] = 0
    search_error: NonEmptyText | None = None

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.queries > self.tool_calls:
            raise ValueError("查询次数不能超过包含查询在内的工具调用次数")
        if len(self.results_per_query) != self.queries:
            raise ValueError("每次查询必须记录候选数，失败查询记为 0")
        return self


class FactClaimState(BaseModel):
    """一条选中 Claim 的累积状态；补搜保留已有证据、判定和轮次记录。

    plan 保存当前计划，补搜不能改变原始主张与目标范围。assessment 为
    最近一次有效判定；error 仅保存导致核验未完成的技术错误。尚未判定
    时 assessment 可为空，Finalize 必须为这种情况产生明确的失败 finding。
    """

    model_config = ConfigDict(extra="forbid")
    plan: FactPlan
    evidence: list[FactEvidence] = Field(default_factory=list, max_length=MAX_ROUNDS * MAX_EVIDENCE_PER_ROUND)
    assessment: FactAssessment | None = None
    rounds: list[FactRoundState] = Field(default_factory=list, max_length=MAX_ROUNDS)
    error: NonEmptyText | None = None

    @model_validator(mode="after")
    def consistent_history(self) -> Self:
        if [item.round_number for item in self.rounds] != list(range(1, len(self.rounds) + 1)):
            raise ValueError("轮次必须从 1 开始连续保存，补搜不能覆盖首轮记录")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("累积证据的 evidence_id 不能重复")
        if self.assessment is not None:
            assessment = self.assessment
            if (assessment.target, assessment.time_scope) != (self.plan.target, self.plan.time_scope):
                raise ValueError("判定的目标和时间范围必须与计划一致")
            references = set(assessment.supporting_evidence + assessment.counter_evidence + assessment.context_evidence)
            if not references <= evidence_ids:
                raise ValueError("判定引用了该 Claim 未取得的证据")
        return self


class FactState(SubgraphState, total=False):
    """Fact 图的通道集合；初始化后再进入各节点的必填 State。

    claim_states 按 claim_id 索引，键必须等于 plan.claim_id，并属于原始
    claims；选中后与 selected_claim_ids 一一对应。active_claim_ids 只包含
    当前轮待处理的主张，不能删除已完成或失败的 claim_states。
    deadline_at 是早于主图硬超时的内部截止时间，为结果组装和清理留余量。
    """

    claim_states: dict[str, FactClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime
    # 执行诊断仅供日志与最终 notes，不属于 PlanState/ValidateState 的模型输入。
    diagnostics: list[str]


class PlanState(SubgraphInput):
    """Plan 读取原始主张、上下文和历史状态。

    首轮 claim_states 为空，active_claim_ids 为所有候选主张；Plan 自行
    选择事实性内容。补搜时只保留证据不足且有时间和可用能力的 active IDs，
    从历史状态读取既有计划、证据、remaining_gaps 和搜索错误。
    """

    claim_states: dict[str, FactClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class PlanUpdate(TypedDict):
    """Plan 校验输出后写回；补搜仅更新活动主张的计划并保留其历史。"""

    selected_claim_ids: list[str]
    active_claim_ids: list[str]
    claim_states: dict[str, FactClaimState]


class SearchState(SubgraphInput):
    """活动主张必须已有计划；Search 只对这些主张执行本轮取证。"""

    claim_states: dict[str, FactClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class SearchUpdate(TypedDict):
    """Search 合并增量证据、追加轮次计数和错误，保留已有判定。"""

    claim_states: dict[str, FactClaimState]


class ValidateState(SubgraphInput):
    """Validate 读取活动主张的计划、所有轮次的证据与取证错误。"""

    claim_states: dict[str, FactClaimState]
    active_claim_ids: list[str]
    deadline_at: datetime


class ValidateUpdate(TypedDict):
    """写入有效 assessment；代码按缺口、轮次、截止时间和能力筛选补搜 IDs。"""

    claim_states: dict[str, FactClaimState]
    active_claim_ids: list[str]


class FinalizeState(SubgraphInput):
    """Finalize 为每条选中 Claim 组装 finding，无需模型或取证工具。

    reason 复制为 summary，引用材料随结果返回；没有有效判定时必须写 error。
    无选择为 skipped；正常结束（包括 UNVERIFIED）为 completed；部分失败
    或补搜失败保留旧判定为 partial；全部执行失败且无有效判定为 failed。
    """

    selected_claim_ids: list[str]
    claim_states: dict[str, FactClaimState]
    notes: list[str]


FinalizeUpdate = SubgraphOutput
