"""Tool-layer contracts: what `lookup_terms` and `verify_translation` return."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from contracts.glossary import TermMatch


class LookupResult(BaseModel):
    """Return value of `lookup_terms`.

    Two consumers, two shapes, on purpose: `matches` is for code (offsets survive
    so callers can do their own de-duplication or highlighting), `glossary_block`
    is a ready-to-paste block for the model. Changing the block's formatting is
    therefore not a schema change.
    """

    model_config = ConfigDict(frozen=True)

    matches: list[TermMatch] = Field(default_factory=list)
    glossary_block: str = ""
    count: int = 0


class Verdict(StrEnum):
    """Per-term outcome of judging a translation. Shared with offline eval."""

    HIT = "HIT"
    WRONG = "WRONG"
    MISS = "MISS"


class TermVerdict(BaseModel):
    """How one source term fared in the produced translation."""

    model_config = ConfigDict(frozen=True)

    zh: str
    expected_en: str
    verdict: Verdict
    found: str | None = None


class VerifyResult(BaseModel):
    """Return value of `verify_translation`.

    `hit_rate` is HIT / total terms; a sentence with no glossary terms scores 1.0
    because there was nothing to get wrong.
    """

    model_config = ConfigDict(frozen=True)

    results: list[TermVerdict] = Field(default_factory=list)
    hit_rate: float = 1.0
    missed: list[str] = Field(default_factory=list)
