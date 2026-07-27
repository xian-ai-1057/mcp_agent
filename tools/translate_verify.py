"""`verify_translation` — did the translation actually use the required terms?

Stateless by design: it reports, it does not decide. Whether a miss is worth
another attempt is the agent loop's call (spec 003 §5), which is what keeps this
tool independently testable and keeps the retry policy out of an MCP schema.
"""

from typing import Any

from contracts.tools import VerifyResult
from glossary.matcher import hit_rate, match_terms, missed_terms
from glossary.runtime import get_glossary
from glossary.scanner import scan
from tools.base import ToolSpec, object_schema

# The offline experiment left 1-3% of terms unhit, all of them in sentence-level
# items where surrounding context pulls the model off the glossary. This tool is
# the check that closes that gap.
DESCRIPTION = """\
Check whether an English translation correctly used the authoritative glossary \
terms found in its Chinese source.

Call this after producing a Chinese-to-English translation to confirm the \
terminology, or when the user asks you to review or check an existing \
translation. Returns a per-term verdict — HIT, WRONG (a different term was used) \
or MISS (the term is absent) — plus the overall hit rate and the list of terms \
that need fixing.\
"""


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    source = arguments.get("source_text") or ""
    translation = arguments.get("translation") or ""
    glossary = get_glossary()

    verdicts = match_terms(translation, scan(source, glossary), glossary)
    result = VerifyResult(
        results=verdicts,
        hit_rate=hit_rate(verdicts),
        missed=missed_terms(verdicts),
    )
    return result.model_dump(mode="json")


SPEC = ToolSpec(
    name="verify_translation",
    description=DESCRIPTION,
    input_schema=object_schema(
        {
            "source_text": {
                "type": "string",
                "description": "The original Chinese text.",
            },
            "translation": {
                "type": "string",
                "description": "The English translation to check.",
            },
        },
        required=["source_text", "translation"],
    ),
    handler=_run,
    tags=("translation", "glossary"),
)
