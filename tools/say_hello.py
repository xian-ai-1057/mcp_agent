"""`say_hello` — a greeting.

The "has parameters, no side effects, no external dependency" corner of the tool
test matrix. Its job in this repository is to be a third distinct *shape* the
registry has to handle, and a third option the model has to route past.
"""

from typing import Any

from tools.base import ToolError, ToolSpec, object_schema

GREETINGS = {
    "zh": "{name}，您好！很高興為您服務。",
    "en": "Hello, {name}! Nice to meet you.",
}
DEFAULT_LANGUAGE = "zh"

DESCRIPTION = """\
Produce a greeting for a named person, in Chinese or English.

Call this only when the user explicitly asks you to greet, welcome or say hello \
to someone by name. Do not call it to open or close an ordinary reply, and do \
not call it for translation requests.\
"""


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    name = (arguments.get("name") or "").strip()
    if not name:
        raise ToolError("name must not be empty")

    language = (arguments.get("language") or DEFAULT_LANGUAGE).strip().lower()
    template = GREETINGS.get(language)
    if template is None:
        raise ToolError(f"unsupported language {language!r}; use 'zh' or 'en'")

    return {"greeting": template.format(name=name), "language": language}


SPEC = ToolSpec(
    name="say_hello",
    description=DESCRIPTION,
    input_schema=object_schema(
        {
            "name": {"type": "string", "description": "Who to greet."},
            "language": {
                "type": "string",
                "enum": ["zh", "en"],
                "description": "Greeting language. Optional; defaults to zh.",
            },
        },
        required=["name"],
    ),
    handler=_run,
    tags=("utility",),
)
