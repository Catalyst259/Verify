from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel, ConfigDict, Discriminator, Field, StringConstraints, Tag, field_validator, model_validator,
)

from pydantic.json_schema import SkipJsonSchema

# 保留已有模型导入路径；提取模型由 extraction 模块定义。
from backend.extraction.models import Claim, ClaimExtractionResult, Source

__all__ = [
    "Claim", "ClaimExtractionResult", "Source", "VerificationInput", "PlaceReference",
    "VerificationContext", "Evidence", "ClaimFinding", "SubgraphResult", "VerificationRun",
    "Assessment", "ClaimAssessment", "FactAssessment", "FactDimensions", "FactGap",
    "FactSourceType", "FactVerdict", "RouteAssessment", "RouteVerdict", "CrowdAssessment",
    "CrowdVerdict", "ExperienceAssessment", "ExperienceVerdict",
]

NonEmptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
FactScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FactSourceType = Literal["OFFICIAL", "CTRIP_PRODUCT", "WEB"]
FactVerdict = Literal["SUPPORTED", "CONTRADICTED", "CONDITIONAL", "UNVERIFIED"]
RouteVerdict = Literal["MATCHED", "MISMATCHED", "CONDITION_MISMATCH", "UNVERIFIED"]
CrowdVerdict = Literal["SUPPORTED", "NOT_SUPPORTED", "SCENARIO_ONLY", "UNVERIFIED"]
ExperienceVerdict = Literal["CONSISTENT", "DIVERGENT", "SCENARIO_DEPENDENT", "UNVERIFIED"]


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


class Assessment(BaseModel):
    """四类子图共用的判定骨架。

    target/time_scope 由代码从计划复制，用于阻止目标范围在判定阶段漂移；confidence 仅表示模型自评。
    verdict 词表由子类收窄；证据不足时不得给出确定结论，因此 verdict 必须为 UNVERIFIED
    且 confidence 必须为 null，这是四类共用的约束。
    """

    model_config = ConfigDict(extra="forbid")
    target: NonEmptyText
    time_scope: NonEmptyText
    verdict: NonEmptyText
    confidence: FactScore | None
    evidence_sufficient: bool
    reason: NonEmptyText
    conditions: list[NonEmptyText] = Field(default_factory=list)
    supporting_evidence: list[NonEmptyText] = Field(default_factory=list)
    counter_evidence: list[NonEmptyText] = Field(default_factory=list)
    context_evidence: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_verdict(self) -> Self:
        if not self.evidence_sufficient and (self.verdict != "UNVERIFIED" or self.confidence is not None):
            raise ValueError("证据不足时 verdict 必须为 UNVERIFIED，confidence 必须为 null")
        return self


class FactAssessment(Assessment):
    """Validate 的结构化判定，也是最终 Finding 中保留的判定。

    Fact 子类追加证据维度与缺口；引用存在性与范围一致性在 Fact Claim 状态中校验。
    kind 参与判别联合但排除序列化，使 Fact 的既有 JSON 结构保持不变。
    """

    kind: SkipJsonSchema[Literal["fact"]] = Field(default="fact", exclude=True)
    verdict: FactVerdict
    dimensions: FactDimensions
    remaining_gaps: list[FactGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_fact_verdict(self) -> Self:
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


class RouteAssessment(Assessment):
    """ROUTE 的判定：主张数值与实测数值的比对，不做真假判断。

    verdict 词表——MATCHED 数值吻合 / MISMATCHED 数值冲突 / CONDITION_MISMATCH 条件不符 /
    UNVERIFIED 证据不足。测量值缺失表示起终点或路线能力不可用，此时只能给 UNVERIFIED。
    """

    kind: Literal["route"] = "route"
    verdict: RouteVerdict
    claimed_seconds: float
    measured_seconds: float | None = None
    transport_mode: NonEmptyText
    distance_meters: float | None = None
    tolerance_seconds: float


class CrowdAssessment(Assessment):
    """CROWD 的判定：主张客流状况在特定场景条件下是否成立。

    verdict 词表——SUPPORTED 当前证据支持 / NOT_SUPPORTED 当前证据不支持 /
    SCENARIO_ONLY 仅特定场景成立 / UNVERIFIED 证据不足。
    scenario 是主张所依赖的星期、节假日、时段、季节；其成立条件由基类 conditions 承载。
    """

    kind: Literal["crowd"] = "crowd"
    verdict: CrowdVerdict
    scenario: NonEmptyText
    evidence_time_coverage: NonEmptyText | None = None


class ExperienceAssessment(Assessment):
    """EXPERIENCE 的判定：来源之间体验的一致程度，不把主观体验判成真假。

    verdict 词表——CONSISTENT 体验较一致 / DIVERGENT 存在明显分化 /
    SCENARIO_DEPENDENT 高度依赖场景 / UNVERIFIED 证据不足。
    """

    kind: Literal["experience"] = "experience"
    verdict: ExperienceVerdict
    source_agreement: FactScore | None


def _assessment_kind(value: object) -> str:
    """判定子类的判别标签；旧数据没有 kind 时按 fact 处理，保住既有 JSON 契约。"""

    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    return kind if isinstance(kind, str) else "fact"


ClaimAssessment = Annotated[
    Annotated[FactAssessment, Tag("fact")] | Annotated[RouteAssessment, Tag("route")]
    | Annotated[CrowdAssessment, Tag("crowd")] | Annotated[ExperienceAssessment, Tag("experience")],
    Discriminator(_assessment_kind),
]


class ClaimFinding(BaseModel):
    claim_id: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    assessment: ClaimAssessment | None = None
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
