"""Validate submitted material identities and apply narrowly scoped source repairs."""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.common.errors import ExtractionFailed, ExtractionSourceMismatch

from .materials import LinkMaterial
from .models import ClaimExtractionResult


class SourceRepairEntry(BaseModel):
    """A correction can change only one source's type and submitted identity."""

    model_config = ConfigDict(extra="forbid")
    claim_index: int = Field(ge=0)
    source_index: int = Field(ge=0)
    source_type: Literal["TEXT", "IMAGE", "LINK"]
    source_ref: str | None


class SourceRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrections: list[SourceRepairEntry] = Field(min_length=1)


def normalize_sources(
    result: ClaimExtractionResult, *, text: str | None, links: Sequence[str],
    image_codes: Sequence[str], link_materials: Sequence[LinkMaterial] = (),
) -> ClaimExtractionResult:
    """Return a copy with canonical source types, or every invalid source position."""
    normalized = result.model_copy(deep=True)
    source_refs = {"IMAGE": set(image_codes), "LINK": set(links)}
    photo_links = {item.original_url for item in link_materials if item.images}
    issues = []
    for claim_index, claim in enumerate(normalized.claims):
        for source_index, source in enumerate(claim.sources):
            # A note photo belongs to its submitted note, never an uploaded file.
            if source.source_type == "IMAGE" and source.source_ref in photo_links:
                source.source_type = "LINK"
            if source.source_type == "TEXT":
                if text and text.strip():
                    source.source_ref = None
                    continue
                reason = "TEXT_MATERIAL_NOT_SUBMITTED"
            elif source.source_ref not in source_refs[source.source_type]:
                reason = f"{source.source_type}_REF_NOT_SUBMITTED"
            else:
                continue
            issues.append({"claim_index": claim_index, "source_index": source_index,
                           "source_type": source.source_type, "reason": reason})
    if issues:
        raise ExtractionSourceMismatch(issues)
    return normalized


def apply_source_repairs(
    result: ClaimExtractionResult, corrections: SourceRepairResult, issues: Sequence[dict], *,
    text: str | None, links: Sequence[str], image_codes: Sequence[str],
    link_materials: Sequence[LinkMaterial] = (),
) -> ClaimExtractionResult:
    """Replace all and only invalid source identities; preserve every original claim."""
    materials = {"text": text, "links": links, "image_codes": image_codes, "link_materials": link_materials}
    failure = "Agent 来源纠正结果与待纠正项不一致"
    try:
        normalize_sources(result, **materials)
    except ExtractionSourceMismatch as current:
        invalid_positions = {(item["claim_index"], item["source_index"]) for item in current.issues}
    else:
        raise ExtractionFailed(failure)
    expected_positions = {(item["claim_index"], item["source_index"]) for item in issues}
    repair_positions = [(item.claim_index, item.source_index) for item in corrections.corrections]
    if (len(expected_positions) != len(issues) or expected_positions != invalid_positions
            or len(set(repair_positions)) != len(repair_positions) or set(repair_positions) != invalid_positions):
        raise ExtractionFailed(failure)
    repaired = result.model_copy(deep=True)
    for entry in corrections.corrections:
        source = repaired.claims[entry.claim_index].sources[entry.source_index]
        source.source_type = entry.source_type
        source.source_ref = entry.source_ref
    return normalize_sources(repaired, **materials)
