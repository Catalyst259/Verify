from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: Literal["TEXT", "IMAGE", "LINK"]
    source_ref: str | None
    source_text: str | None


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    type: Literal["FACT", "ROUTE", "CROWD", "EXPERIENCE"]
    content: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)


class ClaimExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_place: str
    claims: list[Claim]
