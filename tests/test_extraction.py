import asyncio

import pytest

from backend.common.errors import ExtractionFailed
from backend.extraction.models import ClaimExtractionResult
from backend.extraction.service import ClaimExtractionService
from backend.storage.repository import StorageRepository


def test_text_only_can_return_no_claims(tmp_path):
    storage = StorageRepository(tmp_path)
    storage.initialize()

    async def extract(target_place, description_text, links, images):
        assert description_text is None
        assert links == images == []
        return ClaimExtractionResult(target_place=target_place, claims=[])

    result = asyncio.run(ClaimExtractionService(storage, extract).extract("公园", "", [], []))
    assert result.model_dump() == {"target_place": "公园", "claims": []}


def test_agent_cannot_reference_unsubmitted_material(tmp_path):
    storage = StorageRepository(tmp_path)
    storage.initialize()

    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[{
            "claim_id": "claim_001", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "IMAGE", "source_ref": "unsubmitted", "source_text": None}],
        }])

    with pytest.raises(ExtractionFailed, match="未提交的材料"):
        asyncio.run(ClaimExtractionService(storage, extract).extract("公园", "免费开放", [], []))
