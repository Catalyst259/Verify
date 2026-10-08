import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from backend.common.errors import ExtractionFailed, ExtractionTimeout
from backend.storage.repository import StorageRepository

from .models import ClaimExtractionResult
from .materials import LinkMaterial


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
        except TimeoutError as error:
            raise ExtractionTimeout() from error
        result.target_place = target_place
        source_refs = {"IMAGE": set(image), "LINK": set(link)}
        photo_links = {item.original_url for item in link_materials if item.images}
        for number, claim in enumerate(result.claims, 1):
            claim.claim_id = f"claim_{number:03d}"
            for source in claim.sources:
                # 来源类型由实际材料身份决定；笔记配图不是用户上传的 file_code。
                if source.source_type == "IMAGE" and source.source_ref in photo_links:
                    source.source_type = "LINK"
                if source.source_type == "TEXT":
                    if not text.strip():
                        raise ExtractionFailed("Agent 引用了未提交的文字材料")
                    source.source_ref = None
                elif source.source_ref not in source_refs[source.source_type]:
                    raise ExtractionFailed("Agent 返回了未提交的材料来源")
        return result
