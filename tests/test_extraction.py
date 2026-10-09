import asyncio

import pytest

from backend.common.errors import ExtractionFailed
from backend.extraction.models import ClaimExtractionResult
from backend.extraction.materials import LinkMaterial
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


@pytest.mark.parametrize('has_photos,ref,expected', [
    (True, 'submitted', 'LINK'), (False, 'submitted', None), (True, 'unsubmitted', None),
])
def test_note_photos_use_link_identity_even_if_model_calls_them_images(tmp_path, has_photos, ref, expected):
    storage = StorageRepository(tmp_path)
    storage.initialize()
    url = 'https://www.xiaohongshu.com/explore/' + 'a' * 24
    material = LinkMaterial(url, url, '公园', '免费开放', (b'png',) if has_photos else ())

    async def extract(*args, **kwargs):
        return ClaimExtractionResult(target_place='公园', claims=[{
            'claim_id': 'wrong', 'type': 'FACT', 'content': '免费开放',
            'sources': [{'source_type': 'IMAGE', 'source_ref': url if ref == 'submitted' else 'unknown',
                         'source_text': '笔记配图展示免费开放标牌'}],
        }])

    run = ClaimExtractionService(storage, extract).extract('公园', '', [url], [], link_materials=(material,))
    if expected:
        result = asyncio.run(run)
        assert result.claims[0].sources[0].source_type == expected
        assert result.claims[0].sources[0].source_ref == url
    else:
        with pytest.raises(ExtractionFailed, match='未提交'):
            asyncio.run(run)
