"""`lookup_terms` — find the authoritative English for terms in a Chinese sentence."""

from typing import Any

from agent.prompts import format_glossary_block
from contracts.tools import LookupResult
from glossary.runtime import get_glossary
from glossary.scanner import scan
from tools.base import ToolSpec, object_schema

# Written to be read by the model, and to be blunt about ordering: the offline
# experiment measured 42.7% / 49.7% without the glossary against 98.1% / 99.0%
# with it, so the cost of a needless call is far below the cost of a skipped one.
DESCRIPTION = """\
Look up the authoritative English translation of banking and finance terms that \
appear in a Chinese sentence.

Call this FIRST, before writing any Chinese-to-English translation, even when \
you are confident you already know the terms — the glossary overrides your own \
preference and is the only authoritative source. Returns the terms found, with \
their required English, plus a ready-to-use glossary block.

Do not call this for English-to-Chinese translation, and do not call it for \
requests that are not translation requests.\
"""


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments.get("text") or ""
    matches = scan(text, get_glossary())
    result = LookupResult(
        matches=matches,
        glossary_block=format_glossary_block(matches),
        count=len(matches),
    )
    return result.model_dump(mode="json")


SPEC = ToolSpec(
    name="lookup_terms",
    description=DESCRIPTION,
    input_schema=object_schema(
        {
            "text": {
                "type": "string",
                "description": "The Chinese text to be translated, exactly as the user wrote it.",
            }
        },
        required=["text"],
    ),
    handler=_run,
    tags=("translation", "glossary"),
)
