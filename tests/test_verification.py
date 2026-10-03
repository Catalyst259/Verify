import asyncio

import pytest

from backend.storage.repository import StorageRepository
from backend.verification.models import ClaimExtractionResult
from backend.verification.service import VerificationService


def test_text_only_can_return_no_claims(tmp_path):
    storage = StorageRepository(tmp_path)
    storage.initialize()

    async def extract(target_place, description_text, links, images):
        assert description_text is None
        assert links == images == []
        return ClaimExtractionResult(target_place=target_place, claims=[])

    result = asyncio.run(VerificationService(storage, extract).verify("公园", "", [], []))
    assert result.model_dump() == {"target_place": "公园", "claims": []}


def test_agent_cannot_reference_unsubmitted_material(tmp_path):
    storage = StorageRepository(tmp_path)
    storage.initialize()

    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[{
            "claim_id": "claim_001", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "IMAGE", "source_ref": "unsubmitted", "source_text": None}],
        }])

    with pytest.raises(ValueError, match="未提交的材料"):
        asyncio.run(VerificationService(storage, extract).verify("公园", "免费开放", [], []))
