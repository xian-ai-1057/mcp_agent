"""Knowledge-layer contracts: one glossary row, and one hit inside a sentence."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GlossaryEntry(BaseModel):
    """One row of the CSV glossary.

    `zh` is canonical. `aliases` are additional Chinese surface forms that map to
    the same English translation; they are scannable but never reported as the
    term's identity.
    """

    model_config = ConfigDict(frozen=True)

    zh: str
    en: str
    aliases: list[str] = Field(default_factory=list)
    category: str

    @field_validator("zh", "en", "category")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, values: list[str]) -> list[str]:
        seen: list[str] = []
        for value in values:
            stripped = value.strip()
            if stripped and stripped not in seen:
                seen.append(stripped)
        return seen

    @property
    def surfaces(self) -> list[str]:
        """Every Chinese string that should be scanned for, canonical first."""
        return [self.zh, *self.aliases]


class TermMatch(BaseModel):
    """A glossary term found in a source sentence.

    `start`/`end` are character offsets into the *original* text, so the matched
    surface form is recoverable as `text[start:end]` — an alias hit reports the
    canonical `zh` here while the span still points at what was actually written.
    """

    model_config = ConfigDict(frozen=True)

    zh: str
    en: str
    start: int
    end: int

    @field_validator("end")
    @classmethod
    def _ordered(cls, end: int, info) -> int:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("end must be greater than start")
        return end
