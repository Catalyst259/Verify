"""Fact 节点的数据契约；轮次、调用预算与执行错误保存在 state 模块。"""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ...models import Evidence, FactAssessment, FactSourceType, NonEmptyText


FactType = Literal[
    "OPEN_STATUS", "PRICE_POLICY", "RESERVATION", "ACCESS_POLICY", "FACILITY", "TEMPORARY_EVENT",
]

MAX_ROUNDS = 2
MAX_TOOL_CALLS = 20
MAX_QUERIES = 5
MAX_RESULTS_PER_QUERY = 10
MAX_EVIDENCE_PER_ROUND = 15


class EvidenceStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Annotated[int, Field(strict=True, ge=1)]
    source_type: FactSourceType
    purpose: NonEmptyText


class FactPlan(BaseModel):
    """Plan 为一条选中的原始 Claim 生成的取证计划。

    一次调用可返回多条计划，未选中的主张不生成计划；不按 Claim.type
    预先过滤。原文通过 claim_id 从 State 读取。time_scope 必须按核验
    时间与地点表达，无法确定的年份、对象或区域保留在 questions 中。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    fact_type: FactType
    target: NonEmptyText
    time_scope: NonEmptyText
    questions: list[NonEmptyText] = Field(min_length=1)
    evidence_strategy: list[EvidenceStrategy] = Field(min_length=1)


class FactEvidence(Evidence):
    """已由工具读取、由代码分配运行内唯一 ID 的网页正文。

    保留相关原文及上下文，不能用模型摘要代替。published_at 可未知；
    retrieved_at 必须包含时区，由工具记录，不能当作政策生效时间。
    """

    model_config = ConfigDict(extra="forbid")
    source: NonEmptyText
    content: NonEmptyText
    url: NonEmptyText
    evidence_id: NonEmptyText
    source_type: FactSourceType
    retrieved_at: AwareDatetime

    @field_validator("url")
    @classmethod
    def web_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("Fact 证据必须提供完整的 HTTP(S) URL")
        return value


class SearchResult(BaseModel):
    """Search 单条 Claim 的本轮增量；去重合并后才进入 Validate。

    无结果是 evidence=[]、error=null；工具失败可同时返回已取得的材料。
    按 URL 和正文去重，同一 URL 的不同正文保留为不同材料。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    evidence: list[FactEvidence] = Field(max_length=MAX_EVIDENCE_PER_ROUND)
    error: NonEmptyText | None


class ValidateResult(BaseModel):
    """Validate 单条 Claim 的输出；只引用累积材料，不调用取证工具。

    代码从计划复制 assessment.target/time_scope 后，连同全部累积证据
    校验 FactClaimState。remaining_gaps 随 assessment 供下一轮 Plan 读取。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    assessment: FactAssessment
