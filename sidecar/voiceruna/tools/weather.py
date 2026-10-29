"""Open-Meteo weather service (TZ §8.5): geocoding + current forecast.

No API key is needed. Results are cached per city for weather.cache_ttl_sec
(600 s by default). Temperature is spoken as in the TZ example
«В Сургуте сейчас −5, облачно, ветер 3 метра в секунду, влажность 78%,
ощущается как −9» with U+2212 as the minus sign.
"""

from __future__ import annotations

import math
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..logging_setup import get_logger
from ..security import normalize_alias
from .base import ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from ..config import ConfigManager

__all__ = ["WeatherService"]

logger = get_logger("weather")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "wind_speed_10m,weather_code"
)
REQUEST_TIMEOUT_SEC = 10.0
#: Open-Meteo is blocked on some networks (owner's provider drops it with a
#: connect timeout) while wttr.in answers, so it is used as a second source.
WTTR_URL = "https://wttr.in/{city}"
WTTR_TIMEOUT_SEC = 10.0
MAX_CITY_LENGTH = 100
DEFAULT_CACHE_TTL_SEC = 600

# Ask for several matches and pick the best one: with count=1 the geocoder
# answers «Москве» with Moscow, New Mexico (population ~1500) instead of Москва.
GEOCODING_RESULT_COUNT = 8

MINUS_SIGN = "\u2212"

# WMO 4677 weather codes grouped as in TZ §8.5.
_CODE_DESCRIPTIONS: dict[int, str] = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "облачно",
    45: "туман",
    48: "туман",
    51: "морось",
    53: "морось",
    55: "морось",
    56: "ледяная морось",
    57: "ледяная морось",
    61: "дождь",
    63: "дождь",
    65: "дождь",
    66: "ледяной дождь",
    67: "ледяной дождь",
    71: "снег",
    73: "снег",
    75: "снег",
    77: "снег",
    80: "ливень",
    81: "ливень",
    82: "ливень",
    85: "снегопад",
    86: "снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "гроза с градом",
}

_UNKNOWN_WEATHER = "неизвестно"

#: wttr.in sometimes answers the condition in English even with lang=ru.
_EN_TO_RU = {
    "clear": "ясно",
    "sunny": "ясно",
    "partly cloudy": "переменная облачность",
    "cloudy": "облачно",
    "overcast": "пасмурно",
    "mist": "туман",
    "fog": "туман",
    "smog": "смог",
    "haze": "дымка",
    "patchy rain possible": "возможен дождь",
    "light rain": "небольшой дождь",
    "moderate rain": "дождь",
    "heavy rain": "сильный дождь",
    "light drizzle": "морось",
    "patchy snow possible": "возможен снег",
    "light snow": "небольшой снег",
    "moderate snow": "снег",
    "heavy snow": "сильный снег",
    "thundery outbreaks possible": "возможна гроза",
    "patchy light rain with thunder": "гроза с дождём",
    "blizzard": "метель",
    "freezing fog": "ледяной туман",
}

class _WeatherError(Exception):
    """Network or payload failure; translated into 'нет связи'."""


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    """Russian plural form: 1 метр, 2 метра, 5 метров."""
    count = abs(int(number)) % 100
    if 11 <= count <= 19:
        return forms[2]
    last = count % 10
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def _format_number(value: Any) -> int | None:
    """Round a JSON number; None when it is missing or not finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(round(number))


def _number_from_text(value: Any) -> int | None:
    """wttr.in returns numbers as strings ("4", "-1"); convert them."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _format_number(value)
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(round(number))


def _strip_units(value: Any) -> str:
    """«+4°C» / «+-0°C» -> «4» / «-0»."""
    text = str(value or "").replace("°C", "").replace("°", "").strip()
    return text.lstrip("+") or text


def _digits(value: Any) -> str | None:
    """First number in a wttr field («67%», «→17km/h»)."""
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value or ""))
    return match.group(0).replace(",", ".") if match else None


def _spoken_number(number: int) -> str:
    """Render an integer with U+2212 as the minus sign (TZ §8.5)."""
    return str(number).replace("-", MINUS_SIGN)


def _decline_word(word: str) -> str:
    """Put one Russian word into the prepositional case (simple rules)."""
    if not word:
        return word
    lower = word.lower()
    if lower.endswith(("ый", "ий")):
        return word[:-2] + "ом"
    if lower.endswith("а"):
        return word[:-1] + "е"
    if lower.endswith("я"):
        return word[:-1] + "е"
    if lower.endswith("ь"):
        return word[:-1] + "и"
    if lower.endswith("й"):
        return word[:-1] + "е"
    if lower[-1] in "оуыэиеёю":
        return word
    return word + "е"


def prepositional(city: str) -> str:
    """Put a city name into the prepositional case: Сургут -> Сургуте."""
    cleaned = " ".join(city.split())
    if not cleaned:
        return cleaned
    head, separator, tail = cleaned.rpartition(" ")
    if not separator:
        return _decline_word(cleaned)
    return f"{head} {_decline_word(tail)}"


def _city_variants(city: str) -> list[str]:
    """Candidate spellings for a city that the router heard in a case form.

    «какая погода в Москве» reaches the router as 'москве'; the geocoder needs
    'Москва'. A handful of simple endings covers the common live phrases.
    """
    variants: list[str] = [city]
    known = {city.lower()}
    lower = city.lower()
    if lower.endswith(("е", "и")):
        stem = city[:-1]
        for candidate in (stem + "а", stem + "ь", stem):
            if candidate and candidate.lower() not in known:
                variants.append(candidate)
                known.add(candidate.lower())
    return variants[:4]


def _population(item: dict[str, Any]) -> int:
    """Population of a geocoding result; missing or broken values count as 0."""
    value = item.get("population")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return 0
    return int(number)


def _pick_location(results: list[Any], requested: str) -> dict[str, Any] | None:
    """Choose the best geocoding match for the requested name.

    An exact normalized name match wins; otherwise the most populated city
    wins (a missing population counts as 0) and the first result breaks ties.
    Results without coordinates cannot be used and are skipped. The returned
    mapping carries match='exact' when the requested spelling was found
    verbatim, so the caller can prefer it across city case variants.
    """
    candidates = [
        item
        for item in results
        if isinstance(item, dict)
        and _format_number(item.get("latitude")) is not None
        and _format_number(item.get("longitude")) is not None
    ]
    if not candidates:
        return None

    wanted = normalize_alias(requested)
    exact = [
        item for item in candidates if normalize_alias(str(item.get("name") or "")) == wanted
    ]
    pool = exact or candidates

    best = pool[0]
    best_population = _population(best)
    for item in pool[1:]:
        population = _population(item)
        if population > best_population:
            best = item
            best_population = population

    location: dict[str, Any] = {
        "name": str(best.get("name") or requested),
        "latitude": float(best["latitude"]),
        "longitude": float(best["longitude"]),
        "population": best_population,
        "match": "exact" if exact else "population",
    }
    if best.get("id") is not None:
        location["id"] = best["id"]
    return location


class WeatherService:
    """Current weather for a city via Open-Meteo, with a small TTL cache."""

    def __init__(self, config: ConfigManager, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._cache: dict[str, tuple[float, ToolResult]] = {}

    # ------------------------------------------------------------------ public
    async def forecast(self, city: str | None) -> ToolResult:
        """Return a spoken forecast for the city (default city when omitted)."""
        if city is not None and not isinstance(city, str):
            return self._refusal("город должен быть строкой")

        name = " ".join((city or "").split())
        if not name:
            default_city = self._config.get("weather.default_city", "Сургут")
            name = " ".join(str(default_city or "Сургут").split())
        if len(name) > MAX_CITY_LENGTH:
            return self._refusal("слишком длинное название города")

        cache_key = name.lower()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return _copy_result(cached[1])

        result = await self._fetch(name)
        if result.ok:
            ttl = int(self._config.get("weather.cache_ttl_sec", DEFAULT_CACHE_TTL_SEC) or 0)
            if ttl > 0:
                self._cache[cache_key] = (time.monotonic() + ttl, _copy_result(result))
        return result

    def clear_cache(self) -> None:
        self._cache.clear()

    @staticmethod
    def describe(code: int) -> str:
        """Translate a WMO weather code into a short Russian description."""
        try:
            return _CODE_DESCRIPTIONS.get(int(code), _UNKNOWN_WEATHER)
        except (TypeError, ValueError):
            return _UNKNOWN_WEATHER

    # ------------------------------------------------------------------ internals
    async def _fetch(self, name: str) -> ToolResult:
        language = str(self._config.get("weather.language", "ru") or "ru")
        location: dict[str, Any] | None = None
        fallback: dict[str, Any] | None = None
        try:
            # A case form such as «Москве» yields only tiny namesakes (Moscow,
            # New Mexico) on the real geocoder, so keep looking for an exact
            # name match among the case variants and only then settle for the
            # most populated weak match.
            for candidate in _city_variants(name):
                found = await self._geocode(candidate, language)
                if found is None:
                    continue
                if found.get("match") == "exact":
                    location = found
                    break
                if fallback is None or found["population"] > fallback["population"]:
                    fallback = found
            if location is None:
                location = fallback
            if location is None:
                return ToolResult(
                    ok=False,
                    speech=f"Город {name} не нашёл",
                    payload={"city": name},
                    error="city_not_found",
                )
            current = await self._current(location)
        except _WeatherError:
            logger.info("open-meteo недоступен, пробую wttr.in")
            return await self._fetch_wttr(name)

        code = _format_number(current.get("weather_code"))
        speech, payload = self._build_answer(str(location["name"]), current)
        payload.update(
            {
                "city": str(location["name"]),
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "weather_code": code,
            }
        )
        if location.get("id") is not None:
            payload["city_id"] = location["id"]
        return ToolResult(ok=True, speech=speech, payload=payload)

    async def _geocode(self, name: str, language: str) -> dict[str, Any] | None:
        data = await self._get_json(
            GEOCODING_URL,
            {
                "name": name,
                "count": GEOCODING_RESULT_COUNT,
                "language": language,
                "format": "json",
            },
        )
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        return _pick_location(results, name)

    async def _current(self, location: dict[str, Any]) -> dict[str, Any]:
        data = await self._get_json(
            FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": CURRENT_FIELDS,
                "timezone": "auto",
            },
        )
        current = data.get("current")
        if not isinstance(current, dict):
            raise _WeatherError("no current block")
        if _format_number(current.get("temperature_2m")) is None:
            raise _WeatherError("no temperature")
        if current.get("weather_code") is None:
            current = {**current, "weather_code": -1}
        return current

    async def _fetch_wttr(self, name: str) -> ToolResult:
        """Second source: wttr.in (works where open-meteo is blocked).

        The JSON endpoint of wttr.in is throttled on this network, its compact
        text format answers in under a second, so the values are parsed from
        «Пасмурно|+4°C|+-0°C|67%|→17km/h».
        """
        try:
            text = await self._get_text(
                WTTR_URL.format(city=name), {"format": "%C|%t|%f|%h|%w", "lang": "ru"}
            )
            fields = [part.strip() for part in text.strip().split("|")]
            if len(fields) < 4:
                raise _WeatherError("unexpected wttr payload")
            temperature = _number_from_text(_strip_units(fields[1]))
            if temperature is None:
                raise _WeatherError("no temperature")
            wind_kmh = _number_from_text(_digits(fields[4]) if len(fields) > 4 else None)
            current: dict[str, Any] = {
                "temperature_2m": temperature,
                "apparent_temperature": _number_from_text(_strip_units(fields[2])),
                "relative_humidity_2m": _number_from_text(_digits(fields[3])),
                "wind_speed_10m": int(round(wind_kmh / 3.6)) if wind_kmh is not None else None,
                "weather_code": -1,
            }
            description = fields[0].strip().lower() if fields[0] else ""
            description = _EN_TO_RU.get(description, description)
            if description and description.isascii():
                description = _UNKNOWN_WEATHER
        except _WeatherError:
            return ToolResult(
                ok=False,
                speech=str(self._config.get("errors.no_connection_phrase", "нет связи")),
                payload={"city": name},
                error="no_connection",
            )
        speech, payload = self._build_answer(name, current, description or None)
        payload.update({"city": name, "source": "wttr.in", "weather_code": -1})
        return ToolResult(ok=True, speech=speech, payload=payload)

    async def _get_text(self, url: str, params: dict[str, Any]) -> str:
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params, timeout=REQUEST_TIMEOUT_SEC)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
                    response = await client.get(url, params=params)
            response.raise_for_status()
            return str(response.text)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("запрос погоды не удался: %s", type(exc).__name__)
            raise _WeatherError(type(exc).__name__) from exc

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params, timeout=REQUEST_TIMEOUT_SEC)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
                    response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("запрос погоды не удался: %s", type(exc).__name__)
            raise _WeatherError(type(exc).__name__) from exc
        if not isinstance(data, dict):
            raise _WeatherError("unexpected payload")
        return data

    def _build_answer(
        self,
        city: str,
        current: dict[str, Any],
        description: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        temperature = _format_number(current.get("temperature_2m"))
        parts = [f"В {prepositional(city)} сейчас {_spoken_number(temperature or 0)}"]

        code = _format_number(current.get("weather_code"))
        parts.append(description or self.describe(code if code is not None else -1))

        wind = _format_number(current.get("wind_speed_10m"))
        if wind is not None:
            parts.append(f"ветер {abs(wind)} {_plural(abs(wind), ('метр', 'метра', 'метров'))} в секунду")

        humidity = _format_number(current.get("relative_humidity_2m"))
        if humidity is not None:
            parts.append(f"влажность {humidity}%")

        apparent = _format_number(current.get("apparent_temperature"))
        if apparent is not None:
            parts.append(f"ощущается как {_spoken_number(apparent)}")

        payload = {
            "temperature": temperature,
            "apparent_temperature": apparent,
            "humidity": humidity,
            "wind_speed": wind,
            "description": self.describe(code if code is not None else -1),
        }
        return ", ".join(parts), payload

    def _refusal(self, reason: str) -> ToolResult:
        phrase = self._config.get("errors.tool_refused_phrase", "не могу это сделать")
        speech = str(phrase) if isinstance(phrase, str) and phrase.strip() else "не могу это сделать"
        return ToolResult(ok=False, speech=speech, error=f"invalid_args: {reason}")


def _copy_result(result: ToolResult) -> ToolResult:
    return ToolResult(
        ok=result.ok,
        speech=result.speech,
        payload=dict(result.payload),
        error=result.error,
    )
