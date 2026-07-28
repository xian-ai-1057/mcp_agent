"""`get_weather` — the tool with an external dependency.

The plan lists the weather data source as still-open: whether an internal API is
available, or whether an external service needs review. That question is not
resolved here and should not be resolved by an implementation detail, so the
provider is a swappable function and the default makes **no network call at all**.

- `WEATHER_PROVIDER=stub` (default) — deterministic canned reading.
- `WEATHER_PROVIDER=open-meteo` — live call, no API key required.

The tool therefore ships now, the registry gains its third shape (external
dependency), and nothing leaves the process until someone opts in. When the
sourcing decision lands it is a new provider function, not a redesign.
"""

import os
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from tools.base import ToolError, ToolSpec, object_schema

DEFAULT_PROVIDER = "stub"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 10.0

# https://open-meteo.com/en/docs — WMO weather interpretation codes, condensed.
WMO_CONDITIONS = {
    0: "晴",
    1: "大致晴朗",
    2: "多雲時晴",
    3: "陰",
    45: "有霧",
    48: "霧凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "陣雨",
    81: "陣雨",
    82: "強陣雨",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "雷雨伴冰雹",
}

STUB_READINGS = {
    "台北": (29.5, "多雲時晴"),
    "臺北": (29.5, "多雲時晴"),
    "高雄": (31.2, "晴"),
    "台中": (30.1, "多雲"),
    "東京": (26.8, "陰"),
    "紐約": (22.4, "小雨"),
    "倫敦": (18.3, "陰"),
}
STUB_DEFAULT = (25.0, "多雲")

DESCRIPTION = """\
Get the current weather for a city.

Call this when the user asks about the weather, the temperature, or conditions \
right now in a named place. Requires a city name; ask for one if the user did \
not give it.\
"""


def _stub(city: str) -> dict[str, Any]:
    temperature, condition = STUB_READINGS.get(city, STUB_DEFAULT)
    return {
        "city": city,
        "temperature_c": temperature,
        "condition": condition,
        "source": "stub",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _open_meteo(city: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            geo = client.get(
                GEOCODE_URL, params={"name": city, "count": 1, "language": "zh"}
            )
            geo.raise_for_status()
            places = geo.json().get("results") or []
            if not places:
                raise ToolError(f"could not find a place called {city!r}")

            place = places[0]
            forecast = client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,weather_code",
                },
            )
            forecast.raise_for_status()
            current = forecast.json().get("current") or {}
    except httpx.HTTPError as exc:
        raise ToolError(f"weather provider unreachable: {exc}") from exc

    code = current.get("weather_code")
    return {
        "city": place.get("name", city),
        "temperature_c": current.get("temperature_2m"),
        "condition": WMO_CONDITIONS.get(code, f"WMO {code}"),
        "source": "open-meteo",
        "observed_at": current.get("time", ""),
    }


PROVIDERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "stub": _stub,
    "open-meteo": _open_meteo,
}


def _run(arguments: dict[str, Any]) -> dict[str, Any]:
    city = (arguments.get("city") or "").strip()
    if not city:
        raise ToolError("city must not be empty")

    name = os.environ.get("WEATHER_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ToolError(
            f"unknown WEATHER_PROVIDER {name!r}; available: {', '.join(sorted(PROVIDERS))}"
        )
    return provider(city)


SPEC = ToolSpec(
    name="get_weather",
    description=DESCRIPTION,
    input_schema=object_schema(
        {"city": {"type": "string", "description": "City name, e.g. '台北' or 'Tokyo'."}},
        required=["city"],
    ),
    handler=_run,
    tags=("utility", "external"),
)
