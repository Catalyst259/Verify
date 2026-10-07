"""CROWD 节点的数据契约；判定模型复用已冻结的 CrowdAssessment。

拥挤度判定不是无条件真假，而是「在主张给出的场景条件下是否成立」，所以计划必须
把星期、节假日、时段、季节显式保留为 scenario，validate 必须逐条回显它，最后的
判定才可能被用户按自己的出行时间核对。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...models import CrowdAssessment, Evidence, NonEmptyText
from ..facts.model import MAX_EVIDENCE_PER_ROUND, EvidenceStrategy


CrowdType = Literal["QUEUE", "VISITOR_VOLUME", "TICKETING", "RESERVATION", "SEASONALITY"]


class CrowdPlan(BaseModel):
    """Plan 为一条选中的原始 Claim 生成的客流取证计划。

    一次调用可返回多条计划，未选中的主张不生成计划；不按 Claim.type
    预先过滤。原文通过 claim_id 从 State 读取。scenario 是主张依赖的
    星期、节假日、时段或季节；条件依赖的主张（「周末人少」）必须原样保留
    该条件，不能改写成无条件的客流结论。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    crowd_type: CrowdType
    target: NonEmptyText
    time_scope: NonEmptyText
    scenario: NonEmptyText
    questions: list[NonEmptyText] = Field(min_length=1)
    evidence_strategy: list[EvidenceStrategy] = Field(min_length=1)


class ValidateResult(BaseModel):
    """Validate 单条 Claim 的输出；只引用累积材料，不调用取证工具。

    代码按计划的 scenario 复算材料的时间对齐，再校验判定只引用对得上的
    材料，因此 CrowdClaimState 的判定不能靠引用不符场景的证据得出结论。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    assessment: CrowdAssessment


class CrowdSearchResult(BaseModel):
    """Search 单条 Claim 的本轮增量；材料始终以工具的取证记录为准。

    与 Fact 同一协议，但客流近似信号不是网页，所以这里用基类 Evidence
    承载：网页材料仍由网页会话按必填 URL 的规则登记。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    evidence: list[Evidence] = Field(max_length=MAX_EVIDENCE_PER_ROUND)
    error: NonEmptyText | None
