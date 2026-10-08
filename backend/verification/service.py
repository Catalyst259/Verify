from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace

from backend.extraction.models import ClaimExtractionResult
from backend.extraction.service import ClaimExtractionService
from backend.storage.repository import StorageRepository

from .budget import RunBudget
from .capabilities import VerificationCapabilities
from .graph import build_verification_graph
from .models import VerificationInput, VerificationRun
from .subgraphs import VerificationSubgraph


class VerificationService:
    """运行完整核验主图，返回主张、子图结果和整体执行状态。"""

    def __init__(
        self, storage: StorageRepository, extractor: Callable[..., Awaitable[ClaimExtractionResult]],
        *, subgraphs: Mapping[str, VerificationSubgraph] | None = None,
        capabilities: VerificationCapabilities | None = None,
    ):
        self.capabilities = capabilities or VerificationCapabilities()
        self.graph = build_verification_graph(ClaimExtractionService(storage, extractor), subgraphs)

    async def run(self, request: VerificationInput) -> VerificationRun:
        """将业务输入交给主图，解包并返回完整运行结果。"""
        # 槽位与截止时间必须每次运行新建，不能随 capabilities 实例跨运行复用。
        capabilities = replace(self.capabilities, input_urls=tuple(request.link), run_budget=RunBudget())
        output = await self.graph.ainvoke({"request": request}, context=capabilities)
        return output["result"]
