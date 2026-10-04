from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .models import Evidence, PlaceReference


class EvidenceSource(Protocol):
    """证据来源的最小接口，供子图使用，查询策略由子图决定。"""

    async def search(self, query: str) -> list[Evidence]: ...


class WebSearchSource:
    """Web Search 来源占位，接入供应商前调用会明确报错。"""

    async def search(self, query: str) -> list[Evidence]:
        raise NotImplementedError("Web Search 来源尚未接入搜索供应商")


@dataclass(frozen=True)
class VerificationCapabilities:
    """运行时依赖；模型客户端和来源连接不写入图 State。"""

    place_resolver: Callable[[str], Awaitable[PlaceReference | None]] | None = None
    evidence_sources: Mapping[str, EvidenceSource] = field(
        default_factory=lambda: {"web_search": WebSearchSource()}
    )
    subgraph_timeout_seconds: float = 180
