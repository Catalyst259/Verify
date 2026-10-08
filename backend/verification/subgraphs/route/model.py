"""ROUTE 节点的数据契约：主张里的数字由代码持有，模型只做测量与判定。"""

from typing import Annotated
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ...capabilities import Costing
from ...models import Evidence, NonEmptyText, RouteAssessment
from ..skeleton import CategorySpec  # noqa: F401  (re-export for the category's own graph module)

MAX_MEASUREMENTS = 8


class RoutePlan(BaseModel):
    """一条选中主张的测量计划。

    主张里的起终点常是模糊说法——「地铁」不是站名、「机场」可能是多个航站楼——因此拆成两段
    待解析文本。解析不到具体 POI 就只能判证据不足，不得猜测坐标或用直线距离折算时长。
    target/time_scope 与主张一致，判定阶段必须回显同一范围。
    """

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    target: NonEmptyText
    time_scope: NonEmptyText
    origin_text: NonEmptyText
    destination_text: NonEmptyText
    transport_mode: Costing
    claimed_seconds: Annotated[float, Field(gt=0)]
    tolerance_seconds: Annotated[float, Field(ge=0)]
    questions: list[NonEmptyText] = Field(min_length=1)


class RouteEvidence(Evidence):
    """一次实测路线；measured_seconds 为 None 表示该组合不可达或供应商未给出结果，不是零。

    content 是给人读的实测摘要，不是模型转述；distance_meters 缺失同样表示供应商未给出。
    """

    model_config = ConfigDict(extra="forbid")
    evidence_id: NonEmptyText
    origin_name: NonEmptyText
    destination_name: NonEmptyText
    transport_mode: Costing
    measured_seconds: float | None
    distance_meters: float | None
    source: NonEmptyText
    retrieved_at: AwareDatetime


class RouteSearchResult(BaseModel):
    """测量会话单轮的增量；无结果是 evidence=[]、error=null，失败可同时保留已取得的测量。"""

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    evidence: list[RouteEvidence] = Field(max_length=MAX_MEASUREMENTS)
    error: NonEmptyText | None


class RouteValidateResult(BaseModel):
    """Validate 单条主张的输出；只引用累积测量，不调用地图能力。"""

    model_config = ConfigDict(extra="forbid")
    claim_id: NonEmptyText
    assessment: RouteAssessment
