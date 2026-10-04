from collections.abc import Awaitable, Callable, Mapping

from langgraph.graph.state import CompiledStateGraph

from backend.extraction.service import ClaimExtractionService
from backend.storage.repository import StorageRepository

from .capabilities import VerificationCapabilities
from .graph import build_verification_graph
from .models import ClaimExtractionResult, VerificationInput, VerificationRun


class VerificationService:
    """运行完整主图，同时提供现有 HTTP 接口所需的提取响应。"""

    def __init__(
        self, storage: StorageRepository, extractor: Callable[..., Awaitable[ClaimExtractionResult]],
        *, subgraphs: Mapping[str, CompiledStateGraph] | None = None,
        capabilities: VerificationCapabilities | None = None,
    ):
        self.capabilities = capabilities or VerificationCapabilities()
        self.graph = build_verification_graph(ClaimExtractionService(storage, extractor), subgraphs)

    async def run(self, request: VerificationInput) -> VerificationRun:
        """返回包含子图结果的完整运行数据，供内部调用与后续报告使用。"""
        output = await self.graph.ainvoke({"request": request}, context=self.capabilities)
        return output["result"]

    async def verify(
        self, target_place: str, text: str, link: list[str], image: list[str]
    ) -> ClaimExtractionResult:
        run = await self.run(VerificationInput(target_place=target_place, text=text, link=link, image=image))
        return ClaimExtractionResult(target_place=target_place, claims=run.claims)
