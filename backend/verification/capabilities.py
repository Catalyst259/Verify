from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from .budget import RunBudget
from .models import Evidence, PlaceReference
from backend.extraction.materials import LinkMaterial

if TYPE_CHECKING:
    from .subgraphs.facts.search import SearchSession


# 模型调用与取证执行的共用形状：输入提示词与任务，输出模型返回的 JSON 文本。
ModelCall = Callable[[str, str], Awaitable[str]]
# 取证会话由各子图自建，这里只固定「会话 + 提示词 + 任务」的调用形状。
EvidenceSearch = Callable[["SearchSession", str, str], Awaitable[str]]


async def call_llm(system_prompt: str, task: str) -> str:
    from .subgraphs.facts.llm import complete
    return await complete(system_prompt, task)


async def call_search(session: "SearchSession", system_prompt: str, task: str) -> str:
    from .subgraphs.facts.search import run_search
    return await run_search(session, system_prompt, task)


class EvidenceSource(Protocol):
    """证据来源的最小接口，供子图使用，查询策略由子图决定。"""

    async def search(self, query: str) -> list[Evidence]: ...


class WebSearchSource:
    """Web Search 来源占位，接入供应商前调用会明确报错。"""

    async def search(self, query: str) -> list[Evidence]:
        raise NotImplementedError("Web Search 来源尚未接入搜索供应商")


# 交通方式词表，与 RouteAssessment.transport_mode 表达同一含义。
Costing = Literal["pedestrian", "auto", "bicycle"]


@dataclass(frozen=True)
class Coordinates:
    """WGS84 经纬度；地图能力的输入与可达圈的点用同一表示。"""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class RouteResult:
    """单点路线；时长以秒、距离以米计，字段名本身就是单位。

    时长或距离为 None 表示该组合不可达或供应商未给出结果，不是零。
    """

    costing: Costing
    duration_seconds: float | None = None
    distance_meters: float | None = None


@dataclass(frozen=True)
class MatrixResult:
    """多点矩阵；外层按 sources、内层按 targets 排列，None 表示该组合不可达。"""

    costing: Costing
    durations_seconds: tuple[tuple[float | None, ...], ...]
    distances_meters: tuple[tuple[float | None, ...], ...]


@dataclass(frozen=True)
class Isochrone:
    """给定时长内的可达范围；rings 是闭环的点序列，点落在环内即该时长可达。"""

    costing: Costing
    minutes: float
    rings: tuple[tuple[Coordinates, ...], ...]


class MapRouting(Protocol):
    """坐标 → 时间/距离；供应商调用隔离在实现之后，替换实现不动子图。

    判定「步行 N 分钟」只能用 route 的路线时长或 isochrone 的可达圈，
    直线距离折算会把夸大的步行时长误判为成立。
    """

    async def route(self, origin: Coordinates, destination: Coordinates, costing: Costing) -> RouteResult: ...

    async def matrix(
        self, sources: Sequence[Coordinates], targets: Sequence[Coordinates], costing: Costing
    ) -> MatrixResult: ...

    async def isochrone(self, origin: Coordinates, costing: Costing, minutes: float) -> Isochrone: ...


@dataclass(frozen=True)
class VerificationCapabilities:
    """运行时依赖；模型客户端和来源连接不写入图 State。

    place_resolver 返回 None 表示没解析到具体 POI；调用方只能按「证据不足」处理，
    不得据 None 编造坐标或时长。
    """

    place_resolver: Callable[[str], Awaitable[PlaceReference | None]] | None = None
    map_routing: MapRouting | None = None
    evidence_sources: Mapping[str, EvidenceSource] = field(
        default_factory=lambda: {"web_search": WebSearchSource()}
    )
    subgraph_timeout_seconds: float = 300
    # 浏览器槽位与运行截止时间不能跨运行复用，所以每次运行新建；None 表示该调用点不启用运行级预算。
    run_budget: RunBudget | None = None
    llm: ModelCall = call_llm
    # None 表示本次运行没有取证能力；仍可完成规划和缺证据判定。
    search: EvidenceSearch | None = call_search
    input_urls: tuple[str, ...] = ()
    link_materials: tuple[LinkMaterial, ...] = ()
