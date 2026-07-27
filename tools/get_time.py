"""`get_time` — read the clock.

Trivial on purpose. It exists from Phase 1 because a second tool is the minimum
needed to observe whether the model *chooses* a tool rather than always reaching
for the only one available. Without it the demo would be a translation pipeline
wearing an agent costume.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tools.base import ToolError, ToolSpec, object_schema

DEFAULT_TIMEZONE = "Asia/Taipei"
WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# Disambiguated against its sibling on purpose: `服務時間` is a glossary term, so
# "time" appearing in a request is not by itself a reason to read the clock.
DESCRIPTION = """\
Get the current date and time in a given IANA timezone.

Call this whenever the user asks what time it is, what today's date is, or what \
the current time is in some city. Defaults to Asia/Taipei when no timezone is \
given.

This tool reports the clock. It is not for translating Chinese words about time \
or dates into English — a translation request is never a reason to call it.\
"""


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    name = (arguments.get("timezone") or DEFAULT_TIMEZONE).strip()
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ToolError(
            f"unknown timezone {name!r}; use an IANA name such as 'Asia/Taipei' or 'America/New_York'"
        ) from exc

    now = datetime.now(zone)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": name,
        "weekday": WEEKDAYS_ZH[now.weekday()],
        "utc_offset": now.strftime("%z"),
    }


SPEC = ToolSpec(
    name="get_time",
    description=DESCRIPTION,
    input_schema=object_schema(
        {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. 'Asia/Taipei'. Optional; defaults to Asia/Taipei.",
            }
        }
    ),
    handler=_run,
    tags=("utility",),
)
