import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace

from backend.extraction.models import ClaimExtractionResult
from backend.common.errors import LinkReadError
from backend.extraction.service import ClaimExtractionService
from backend.storage.repository import StorageRepository

from .budget import RunBudget, remaining
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
        # 来源可复用本次运行已读取的正文；缓存不能随应用级来源跨请求沿用。
        sources = {name: source.for_run() if callable(getattr(source, "for_run", None)) else source
                   for name, source in self.capabilities.evidence_sources.items()}
        capabilities = replace(self.capabilities, input_urls=tuple(request.link), run_budget=RunBudget(),
                               evidence_sources=sources)
        materials = []
        for index, url in enumerate(request.link, 1):
            reader = getattr(sources.get("xiaohongshu"), "read_note", None)
            if not callable(reader):
                raise LinkReadError("小红书链接读取能力未配置", 503)
            try:
                async with asyncio.timeout(remaining(capabilities.run_budget.deadline_at)):
                    materials.append(await reader(url, deadline_at=capabilities.run_budget.deadline_at))
            except TimeoutError:
                raise LinkReadError(f"第 {index} 条小红书链接读取超时，请减少材料后重试", 504) from None
            except LinkReadError as error:
                raise LinkReadError(f"第 {index} 条小红书链接：{error}", error.status_code) from None
        capabilities = replace(capabilities, link_materials=tuple(materials),
                               input_urls=(*request.link, *(item.canonical_url for item in materials)))
        output = await self.graph.ainvoke({"request": request}, context=capabilities)
        return output["result"]
