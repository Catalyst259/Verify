from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

# 保留已有模型导入路径；提取模型由 extraction 模块定义。
from backend.extraction.models import Claim, ClaimExtractionResult, Source

__all__ = [
    "Claim", "ClaimExtractionResult", "Source", "VerificationInput", "PlaceReference",
    "VerificationContext", "Evidence", "ClaimFinding", "SubgraphResult", "VerificationRun",
    "FactAssessment", "FactDimensions", "FactGap", "FactSourceType", "FactVerdict",
]

NonEmptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
FactScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FactSourceType = Literal["OFFICIAL", "CTRIP_PRODUCT", "WEB"]
FactVerdict = Literal["SUPPORTED", "CONTRADICTED", "CONDITIONAL", "UNVERIFIED"]


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_place: str
    text: str = ""
    link: list[str] = Field(default_factory=list)
    image: list[str] = Field(default_factory=list)


class PlaceReference(BaseModel):
    """地点来源返回的标识；未接入地点解析时保持为空。"""

    name: str
    reference: str | None = None


class VerificationContext(BaseModel):
    target_place: str
    checked_at: datetime
    resolved_place: PlaceReference | None = None


class Evidence(BaseModel):
    """核验来源材料；元数据可缺省以兼容其他子图和旧来源。

    published_at 保留来源的日期精度，不代表政策生效时间。
    Fact 网页材料的必填约束由 facts.model.FactEvidence 定义。
    """

    source: str
    content: str
    url: str | None = None
    evidence_id: str | None = None
    source_type: str | None = None
    published_at: date | datetime | None = None
    retrieved_at: datetime | None = None

    @field_validator("published_at", mode="before")
    @classmethod
    def preserve_publication_precision(cls, value):
        """按原始字符串区分日期和时间戳，避免联合类型把零点时间截成日期。"""
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return datetime.fromisoformat(value)
        return value


class FactDimensions(BaseModel):
    """五项证据质量维度；未知为 null，不能用平均分代替事实判断。"""

    model_config = ConfigDict(extra="forbid")
    authority: FactScore | None
    directness: FactScore | None
    recency: FactScore | None
    context_match: FactScore | None
    independence: FactScore | None


class FactGap(BaseModel):
    """影响当前结论、需要下一轮取证的问题。"""

    model_config = ConfigDict(extra="forbid")
    question: NonEmptyText
    preferred_source: FactSourceType
    reason: NonEmptyText


class FactAssessment(BaseModel):
    """Validate 的结构化判定，也是最终 Finding 中保留的判定。

    target/time_scope 由代码从计划复制。引用存在性与范围一致性在
    Fact Claim 状态中校验；confidence 仅表示模型自评。
    """

    model_config = ConfigDict(extra="forbid")
    target: NonEmptyText
    time_scope: NonEmptyText
    verdict: FactVerdict
    confidence: FactScore | None
    evidence_sufficient: bool
    reason: NonEmptyText
    conditions: list[NonEmptyText] = Field(default_factory=list)
    supporting_evidence: list[NonEmptyText] = Field(default_factory=list)
    counter_evidence: list[NonEmptyText] = Field(default_factory=list)
    context_evidence: list[NonEmptyText] = Field(default_factory=list)
    dimensions: FactDimensions
    remaining_gaps: list[FactGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_verdict(self) -> Self:
        if not self.evidence_sufficient and (self.verdict != "UNVERIFIED" or self.confidence is not None):
            raise ValueError("证据不足时 verdict 必须为 UNVERIFIED，confidence 必须为 null")
        if self.verdict == "SUPPORTED" and not self.supporting_evidence:
            raise ValueError("SUPPORTED 必须引用支持证据")
        if self.verdict == "CONTRADICTED" and not self.counter_evidence:
            raise ValueError("CONTRADICTED 必须引用反证")
        if self.verdict == "CONDITIONAL":
            if not self.conditions:
                raise ValueError("CONDITIONAL 必须说明成立条件或例外")
            if not (self.supporting_evidence or self.counter_evidence):
                raise ValueError("CONDITIONAL 必须引用支持证据或反证")
        return self


class ClaimFinding(BaseModel):
    claim_id: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    assessment: FactAssessment | None = None
    error: str | None = None


class SubgraphResult(BaseModel):
    """一个子图处理整批主张后的输出，允许每条主张有多项发现。"""

    model_config = ConfigDict(extra="forbid")
    graph_name: str
    status: Literal["completed", "partial", "skipped", "not_implemented", "failed"]
    selected_claim_ids: list[str] = Field(default_factory=list)
    findings: list[ClaimFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class VerificationRun(BaseModel):
    """主图运行结果；status 描述执行情况，不代表主张真实性。"""

    run_id: str
    context: VerificationContext
    claims: list[Claim]
    subgraph_results: dict[str, SubgraphResult]
    status: Literal["no_claims", "not_implemented", "completed", "partial", "failed"]
