import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from backend.common.errors import ExtractionFailed, ExtractionTimeout
from backend.storage.repository import StorageRepository

from .models import ClaimExtractionResult
from .materials import LinkMaterial
from .provenance import normalize_sources


class ClaimExtractionService:
    """读取材料并提取主张，保证返回来源属于本次提交。"""

    def __init__(self, storage: StorageRepository, extractor: Callable[..., Awaitable[ClaimExtractionResult]]):
        self.storage = storage
        self.extractor = extractor

    async def extract(
        self, target_place: str, text: str, link: list[str], image: list[str], *,
        link_materials: tuple[LinkMaterial, ...] = (), deadline_at: datetime | None = None,
    ) -> ClaimExtractionResult:
        try:
            timeout = max(0, (deadline_at - datetime.now(timezone.utc)).total_seconds()) if deadline_at else None
            async with asyncio.timeout(timeout):
                images = await asyncio.to_thread(self.storage.get_images, image)
                if link:
                    if [item.original_url for item in link_materials] != link:
                        raise ExtractionFailed("链接材料未完整读取")
                    result = await self.extractor(target_place, text or None, link, images,
                                                  link_materials=link_materials)
                else:
                    result = await self.extractor(target_place, text or None, link, images)
                result = normalize_sources(result, text=text, links=link, image_codes=image,
                                           link_materials=link_materials)
        except TimeoutError as error:
            raise ExtractionTimeout() from error
        result.target_place = target_place
        for number, claim in enumerate(result.claims, 1):
            claim.claim_id = f"claim_{number:03d}"
        return result
