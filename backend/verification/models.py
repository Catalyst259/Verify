from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 保留已有模型导入路径；提取模型由 extraction 模块定义。
from backend.extraction.models import Claim, ClaimExtractionResult, Source

__all__ = [
    "Claim", "ClaimExtractionResult", "Source", "VerificationInput", "PlaceReference",
    "VerificationContext", "Evidence", "ClaimFinding", "SubgraphResult", "VerificationRun",
]


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
    """核验来源提供的材料，与 Claim 的原始材料来源分别保存。"""

    source: str
    content: str
    url: str | None = None


class ClaimFinding(BaseModel):
    claim_id: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)


class SubgraphResult(BaseModel):
    """一个子图处理整批主张后的输出，允许每条主张有多项发现。"""

    model_config = ConfigDict(extra="forbid")
    graph_name: str
    status: Literal["completed", "skipped", "not_implemented", "failed"]
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
