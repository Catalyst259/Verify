import asyncio
from collections.abc import Awaitable, Callable

from backend.storage.repository import StorageRepository
from .models import ClaimExtractionResult


class VerificationService:
    def __init__(self, storage: StorageRepository, extractor: Callable[..., Awaitable[ClaimExtractionResult]]):
        self.storage = storage
        self.extractor = extractor

    async def verify(
        self, target_place: str, text: str, link: list[str], image: list[str]
    ) -> ClaimExtractionResult:
        images = await asyncio.to_thread(self.storage.get_images, image)
        result = await self.extractor(target_place, text or None, link, images)
        result.target_place = target_place
        source_refs = {"IMAGE": set(image), "LINK": set(link)}
        for number, claim in enumerate(result.claims, 1):
            claim.claim_id = f"claim_{number:03d}"
            for source in claim.sources:
                if source.source_type == "TEXT":
                    if not text.strip():
                        raise ValueError("Agent 引用了未提交的文字材料")
                    source.source_ref = None
                elif source.source_ref not in source_refs[source.source_type]:
                    raise ValueError("Agent 返回了未提交的材料来源")
        return result
