"""Source correction keeps material identity and the original extracted claims."""

import asyncio

import pytest
from pydantic import ValidationError

from backend.common.errors import ExtractionFailed, ExtractionSourceMismatch
from backend.extraction.materials import LinkMaterial
from backend.extraction.models import ClaimExtractionResult
from backend.extraction.provenance import (
    SourceRepairEntry, SourceRepairResult, apply_source_repairs, normalize_sources,
)
from backend.extraction.service import ClaimExtractionService
from backend.storage.repository import StorageRepository


NOTE_URL = "https://www.xiaohongshu.com/explore/" + "a" * 24
SHORT_URL = "https://xhslink.com/a/synthetic-note"


def source(kind="IMAGE", ref="uploaded-code", text="材料中的开放标牌"):
    return {"source_type": kind, "source_ref": ref, "source_text": text}


def result(*sources):
    return ClaimExtractionResult(target_place="公园", claims=[{
        "claim_id": f"original-{index}", "type": "FACT", "content": f"第{index}个入口免费开放。",
        "sources": values,
    } for index, values in enumerate(sources)])


def repair(*values):
    return SourceRepairResult(corrections=[SourceRepairEntry(**value) for value in values])


def materials(**updates):
    return {"text": "公园免费开放。", "links": [NOTE_URL], "image_codes": ["uploaded-code"], **updates}


def test_normalization_returns_deep_copy_and_preserves_all_claims():
    original = result([source("TEXT", "description"), source(), source("LINK", NOTE_URL)])
    before = original.model_dump()
    normalized = normalize_sources(original, **materials())
    assert normalized is not original
    assert normalized.claims[0] is not original.claims[0]
    assert normalized.claims[0].sources[0].source_ref is None
    assert normalized.claims[0].sources[1:] == original.claims[0].sources[1:]
    assert original.model_dump() == before


def test_mismatch_collects_all_positions_without_recording_material_refs():
    original = result([source("TEXT", "synthetic-private-text"), source("IMAGE", "synthetic-private-ref")],
                      [source("LINK", "https://synthetic-private.test/note")])
    with pytest.raises(ExtractionSourceMismatch) as caught:
        normalize_sources(original, **materials(text=None))
    assert caught.value.issues == (
        {"claim_index": 0, "source_index": 0, "source_type": "TEXT", "reason": "TEXT_MATERIAL_NOT_SUBMITTED"},
        {"claim_index": 0, "source_index": 1, "source_type": "IMAGE", "reason": "IMAGE_REF_NOT_SUBMITTED"},
        {"claim_index": 1, "source_index": 0, "source_type": "LINK", "reason": "LINK_REF_NOT_SUBMITTED"},
    )
    assert str(caught.value) == "Agent 返回了未提交的材料来源"
    assert "synthetic-private" not in str(caught.value) + repr(caught.value.issues)


@pytest.mark.parametrize("has_photos", [False, True])
def test_only_actual_note_photos_can_normalize_image_identity_to_link(has_photos):
    original = result([source("IMAGE", NOTE_URL)])
    note = LinkMaterial(NOTE_URL, NOTE_URL, "标题", "正文", (b"synthetic-png",) if has_photos else ())
    kwargs = materials(link_materials=(note,))
    if has_photos:
        normalized = normalize_sources(original, **kwargs)
        assert normalized.claims[0].sources[0].source_type == "LINK"
        assert original.claims[0].sources[0].source_type == "IMAGE"
    else:
        with pytest.raises(ExtractionSourceMismatch):
            normalize_sources(original, **kwargs)


def test_link_reference_must_use_submitted_original_url():
    original = result([source("LINK", NOTE_URL)])
    note = LinkMaterial(SHORT_URL, NOTE_URL, "标题", "正文")
    with pytest.raises(ExtractionSourceMismatch) as caught:
        normalize_sources(original, **materials(links=[SHORT_URL], link_materials=(note,)))
    normalized = apply_source_repairs(original, repair({
        "claim_index": 0, "source_index": 0, "source_type": "LINK", "source_ref": SHORT_URL,
    }), caught.value.issues, **materials(links=[SHORT_URL], link_materials=(note,)))
    assert normalized.claims[0].sources[0].source_ref == SHORT_URL


def test_repair_keeps_legal_claims_and_sources_and_changes_only_invalid_identity():
    original = result([source("TEXT", None)], [source(), source("IMAGE", "wrong-code"), source("LINK", NOTE_URL)])
    before = original.model_dump()
    with pytest.raises(ExtractionSourceMismatch) as caught:
        normalize_sources(original, **materials())
    normalized = apply_source_repairs(original, repair({
        "claim_index": 1, "source_index": 1, "source_type": "IMAGE", "source_ref": "uploaded-code",
    }), caught.value.issues, **materials())
    assert normalized.claims[0] == original.claims[0]
    repaired = normalized.claims[1]
    assert (repaired.claim_id, repaired.type, repaired.content) == (
        original.claims[1].claim_id, original.claims[1].type, original.claims[1].content,
    )
    assert repaired.sources[0] == original.claims[1].sources[0]
    assert repaired.sources[2] == original.claims[1].sources[2]
    assert repaired.sources[1].source_ref == "uploaded-code"
    assert repaired.sources[1].source_text == original.claims[1].sources[1].source_text
    assert original.model_dump() == before


@pytest.mark.parametrize("positions", [
    [(0, 0)],                         # Missing one invalid source.
    [(0, 0), (0, 0), (0, 1)],         # Duplicate invalid source.
    [(0, 0), (0, 1), (1, 0)],         # Unknown claim.
    [(0, 0), (0, 1), (0, 2)],         # A legal source must not be rewritten.
])
def test_repairs_require_exact_invalid_position_coverage(positions):
    original = result([source("IMAGE", "wrong-a"), source("IMAGE", "wrong-b"), source("TEXT", None)])
    with pytest.raises(ExtractionSourceMismatch) as caught:
        normalize_sources(original, **materials())
    corrections = repair(*({"claim_index": claim, "source_index": item,
                            "source_type": "IMAGE", "source_ref": "uploaded-code"}
                           for claim, item in positions))
    with pytest.raises(ExtractionFailed, match="来源纠正"):
        apply_source_repairs(original, corrections, caught.value.issues, **materials())


def test_source_repair_is_revalidated_and_does_not_guess_unknown_refs():
    original = result([source("IMAGE", "wrong-code")])
    with pytest.raises(ExtractionSourceMismatch) as caught:
        normalize_sources(original, **materials())
    with pytest.raises(ExtractionSourceMismatch):
        apply_source_repairs(original, repair({
            "claim_index": 0, "source_index": 0, "source_type": "IMAGE", "source_ref": "still-wrong",
        }), caught.value.issues, **materials())


def test_text_without_material_remains_rejected_after_repair():
    original = result([source("TEXT", None)])
    with pytest.raises(ExtractionSourceMismatch, match="未提交的文字材料") as caught:
        normalize_sources(original, **materials(text="  "))
    with pytest.raises(ExtractionSourceMismatch, match="未提交的文字材料"):
        apply_source_repairs(original, repair({
            "claim_index": 0, "source_index": 0, "source_type": "TEXT", "source_ref": None,
        }), caught.value.issues, **materials(text="  "))


@pytest.mark.parametrize("entry", [
    {"claim_index": -1, "source_index": 0, "source_type": "TEXT", "source_ref": None},
    {"claim_index": 0, "source_index": -1, "source_type": "TEXT", "source_ref": None},
    {"claim_index": 0, "source_index": 0, "source_type": "OTHER", "source_ref": None},
    {"claim_index": 0, "source_index": 0, "source_type": "TEXT", "source_ref": None, "source_text": "改写"},
    {"claim_index": 0, "source_index": 0, "source_type": "TEXT", "source_ref": None, "content": "改写"},
])
def test_repair_schema_rejects_invalid_indices_types_or_extra_claim_fields(entry):
    with pytest.raises(ValidationError):
        SourceRepairResult(corrections=[entry])


def test_repair_schema_rejects_empty_or_extra_result_fields():
    with pytest.raises(ValidationError):
        SourceRepairResult(corrections=[])
    with pytest.raises(ValidationError):
        SourceRepairResult(corrections=[{"claim_index": 0, "source_index": 0,
                                         "source_type": "TEXT", "source_ref": None}], claims=[])


def test_service_final_guard_normalizes_without_mutating_extractor_result(tmp_path):
    storage = StorageRepository(tmp_path)
    storage.initialize()
    original = result([source("TEXT", "description")])
    before = original.model_dump()

    async def extractor(*args):
        return original

    normalized = asyncio.run(ClaimExtractionService(storage, extractor).extract("目标公园", "免费开放", [], []))
    assert normalized.target_place == "目标公园"
    assert normalized.claims[0].claim_id == "claim_001"
    assert normalized.claims[0].sources[0].source_ref is None
    assert original.model_dump() == before
