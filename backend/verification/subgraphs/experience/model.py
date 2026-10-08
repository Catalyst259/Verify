"""Experience 节点的数据契约；取证材料与轮次计数沿用共享的网页取证会话。

体验判定只表达来源之间的一致程度，没有真假之分，因此判定模型直接复用
已冻结的 ExperienceAssessment，这里只定义计划与 Validate 的输入输出。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...models import ExperienceAssessment, NonEmptyText
from ..facts.model import EvidenceStrategy


ExperienceType = Literal["AMBIENCE", "COMFORT", "SERVICE", "SCENERY", "FOOD", "VALUE"]


class ExperiencePlan(BaseModel):
    """Plan 为一条选中的原始 Claim 生成的体验取证计划。

    一次调用可返回多条计划，未选中的主张不生成计划；不按 Claim.type
    预先过滤。原文通过 claim_id 从 State 读取。dimensions 把主张拆成
    可分别核验的体验维度（如「隔音好」拆成噪音、隔音、房型差异），
    questions 逐项覆盖这些维度及其反面体验。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    experience_type: ExperienceType
    target: NonEmptyText
    time_scope: NonEmptyText
    dimensions: list[NonEmptyText] = Field(min_length=1)
    questions: list[NonEmptyText] = Field(min_length=1)
    evidence_strategy: list[EvidenceStrategy] = Field(min_length=1)


class ValidateResult(BaseModel):
    """Validate 单条 Claim 的输出；只引用累积材料，不调用取证工具。

    体验判定没有缺口字段，补搜方向由下一轮 Plan 从已取材料和上次
    assessment 推断，因此这里只回传判定本身。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    assessment: ExperienceAssessment
