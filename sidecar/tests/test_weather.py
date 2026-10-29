"""Tests for the Open-Meteo weather service (TZ §8.5).

All HTTP goes through httpx.MockTransport: no network access, no API keys.
"""

from __future__ import annotations

import httpx
import pytest

from voiceruna.tools.weather import WeatherService

GEOCODING_HOST = "geocoding-api.open-meteo.com"
FORECAST_HOST = "api.open-meteo.com"

SURGUT = {"name": "Сургут", "latitude": 61.25, "longitude": 73.42, "country": "Россия"}
MOSCOW = {"name": "Москва", "latitude": 55.75, "longitude": 37.62, "country": "Россия"}

# Real Open-Meteo trap: «Москве» also matches Moscow, New Mexico from count=1.
MOSCOW_NM = {
    "id": 5480275,
    "name": "Moscow",
    "latitude": 35.06,
    "longitude": -106.32,
    "country": "США",
    "population": 1516,
}
MOSCOW_RU = {
    "id": 524901,
    "name": "Москва",
    "latitude": 55.75,
    "longitude": 37.62,
    "country": "Россия",
    "population": 10400000,
}

CURRENT = {
    "temperature_2m": -5.2,
    "apparent_temperature": -9.1,
    "relative_humidity_2m": 78,
    "wind_speed_10m": 2.6,
    "weather_code": 3,
}


def make_transport(
    requests: list[httpx.Request],
    *,
    results: dict | None = None,
    current: dict | None = None,
    fail: bool = False,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if fail:
            raise httpx.ConnectError("boom", request=request)
        if request.url.host == GEOCODING_HOST:
            if results is None:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json=results)
        return httpx.Response(200, json={"current": current if current is not None else CURRENT})

    return httpx.MockTransport(handler)


async def test_forecast_builds_the_tz_sentence(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        service = WeatherService(config, client=client)
        result = await service.forecast("Сургут")

    assert result.ok is True
    assert result.speech == (
        "В Сургуте сейчас \u22125, облачно, ветер 3 метра в секунду, "
        "влажность 78%, ощущается как \u22129"
    )
    assert "\u2212" in result.speech
    assert "-" not in result.speech

    assert result.payload["city"] == "Сургут"
    assert result.payload["temperature"] == -5
    assert result.payload["apparent_temperature"] == -9
    assert result.payload["humidity"] == 78
    assert result.payload["wind_speed"] == 3
    assert result.payload["weather_code"] == 3
    assert result.payload["description"] == "облачно"


async def test_request_parameters(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        await WeatherService(config, client=client).forecast("Сургут")

    geocoding, forecast = requests
    assert geocoding.url.host == GEOCODING_HOST
    assert geocoding.url.params["name"] == "Сургут"
    assert geocoding.url.params["count"] == "8"
    assert geocoding.url.params["language"] == "ru"
    assert geocoding.url.params["format"] == "json"

    assert forecast.url.host == FORECAST_HOST
    assert forecast.url.params["latitude"] == "61.25"
    assert forecast.url.params["longitude"] == "73.42"
    assert forecast.url.params["timezone"] == "auto"
    assert forecast.url.params["current"] == (
        "temperature_2m,apparent_temperature,relative_humidity_2m,"
        "wind_speed_10m,weather_code"
    )


async def test_cache_avoids_the_second_request(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        service = WeatherService(config, client=client)
        first = await service.forecast("Сургут")
        second = await service.forecast("Сургут")

    assert first.speech == second.speech
    assert len(requests) == 2


async def test_cache_respects_ttl(config) -> None:
    config.set("weather.cache_ttl_sec", 0)
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        service = WeatherService(config, client=client)
        await service.forecast("Сургут")
        await service.forecast("Сургут")

    assert len(requests) == 4


async def test_city_not_found(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": []})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Абвгд")

    assert result.ok is False
    assert result.speech == "Город Абвгд не нашёл"
    assert result.error == "city_not_found"


async def test_default_city_is_used(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast(None)

    assert result.ok is True
    assert result.speech.startswith("В Сургуте сейчас")
    assert requests[0].url.params["name"] == "Сургут"


async def test_prepositional_city_form_is_retried(config) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == GEOCODING_HOST:
            name = request.url.params["name"].lower()
            if name == "москва":
                return httpx.Response(200, json={"results": [MOSCOW]})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"current": CURRENT})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WeatherService(config, client=client).forecast("москве")

    assert result.ok is True
    assert result.speech.startswith("В Москве")


async def test_most_populated_match_beats_the_first_result(config) -> None:
    """The populated Москва wins, not the first tiny namesake."""
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [MOSCOW_NM, MOSCOW_RU]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Москве")

    assert result.ok is True
    assert result.payload["city_id"] == 524901
    assert result.payload["city"] == "Москва"
    assert result.speech.startswith("В Москве")

    geocoding = [r for r in requests if r.url.host == GEOCODING_HOST]
    assert geocoding
    assert all(r.url.params["count"] == "8" for r in geocoding)
    forecast = [r for r in requests if r.url.host == FORECAST_HOST][-1]
    assert forecast.url.params["latitude"] == "55.75"
    assert forecast.url.params["longitude"] == "37.62"


async def test_case_form_retries_until_exact_match(config) -> None:
    """Live geocoder: «Москве» returns only Москверо/Москвец, «Москва» is exact."""
    tiny = {"id": 5480275, "name": "Москверо", "latitude": 35.77, "longitude": -103.95, "population": 93}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == GEOCODING_HOST:
            name = request.url.params["name"].lower()
            if name == "москве":
                return httpx.Response(200, json={"results": [tiny]})
            if name == "москва":
                return httpx.Response(200, json={"results": [MOSCOW_RU]})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"current": CURRENT})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WeatherService(config, client=client).forecast("Москве")

    assert result.ok is True
    assert result.payload["city_id"] == 524901
    assert result.speech.startswith("В Москве")
    names = [r.url.params["name"] for r in requests if r.url.host == GEOCODING_HOST]
    assert names == ["Москве", "Москва"]
    assert requests[-1].url.host == FORECAST_HOST


async def test_exact_name_match_beats_population(config) -> None:
    exact = {"id": 1, "name": "Сургут", "latitude": 61.25, "longitude": 73.42, "population": 100}
    populous = {"id": 2, "name": "Сургутово", "latitude": 1.0, "longitude": 2.0, "population": 9999999}
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [populous, exact]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Сургут")

    assert result.payload["city_id"] == 1
    assert result.payload["city"] == "Сургут"


async def test_exact_match_ignores_yo_spelling(config) -> None:
    exact = {"id": 3, "name": "Орёл", "latitude": 52.97, "longitude": 36.07, "population": 100}
    populous = {"id": 4, "name": "Орловка", "latitude": 1.0, "longitude": 2.0, "population": 9999999}
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [populous, exact]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Орел")

    assert result.payload["city_id"] == 3
    assert result.payload["city"] == "Орёл"


async def test_missing_population_keeps_the_first_result(config) -> None:
    first = {"id": 10, "name": "Альфа", "latitude": 1.0, "longitude": 2.0}
    second = {"id": 11, "name": "Бета", "latitude": 3.0, "longitude": 4.0}
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [first, second]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Гамма")

    assert result.payload["city_id"] == 10
    assert result.payload["city"] == "Альфа"
    assert requests[1].url.params["latitude"] == "1.0"


async def test_present_population_wins_over_missing(config) -> None:
    no_population = {"id": 20, "name": "Гамма", "latitude": 1.0, "longitude": 2.0}
    populated = {"id": 21, "name": "Дельта", "latitude": 3.0, "longitude": 4.0, "population": 500}
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [no_population, populated]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Эпсилон")

    assert result.payload["city_id"] == 21
    assert result.payload["city"] == "Дельта"


async def test_network_error_speaks_no_connection(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, fail=True)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Сургут")

    assert result.ok is False
    assert result.speech == config.get("errors.no_connection_phrase")
    assert result.error == "no_connection"


async def test_long_city_is_refused_without_a_request(config) -> None:
    requests: list[httpx.Request] = []
    transport = make_transport(requests, results={"results": [SURGUT]})

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("а" * 101)

    assert result.ok is False
    assert result.speech == config.get("errors.tool_refused_phrase")
    assert requests == []


@pytest.mark.parametrize(
    ("wind", "phrase"),
    [
        (1.0, "ветер 1 метр в секунду"),
        (3.0, "ветер 3 метра в секунду"),
        (5.0, "ветер 5 метров в секунду"),
        (21.0, "ветер 21 метр в секунду"),
        (0.4, "ветер 0 метров в секунду"),
    ],
)
async def test_wind_plural_forms(config, wind: float, phrase: str) -> None:
    requests: list[httpx.Request] = []
    current = {**CURRENT, "wind_speed_10m": wind}
    transport = make_transport(requests, results={"results": [SURGUT]}, current=current)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await WeatherService(config, client=client).forecast("Сургут")

    assert phrase in result.speech


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "ясно"),
        (1, "преимущественно ясно"),
        (2, "переменная облачность"),
        (3, "облачно"),
        (45, "туман"),
        (48, "туман"),
        (51, "морось"),
        (53, "морось"),
        (55, "морось"),
        (56, "ледяная морось"),
        (57, "ледяная морось"),
        (61, "дождь"),
        (63, "дождь"),
        (65, "дождь"),
        (66, "ледяной дождь"),
        (67, "ледяной дождь"),
        (71, "снег"),
        (73, "снег"),
        (75, "снег"),
        (77, "снег"),
        (80, "ливень"),
        (81, "ливень"),
        (82, "ливень"),
        (85, "снегопад"),
        (86, "снегопад"),
        (95, "гроза"),
        (96, "гроза с градом"),
        (99, "гроза с градом"),
        (42, "неизвестно"),
        (-1, "неизвестно"),
    ],
)
def test_describe(code: int, expected: str) -> None:
    assert WeatherService.describe(code) == expected


def test_prepositional_forms() -> None:
    from voiceruna.tools.weather import prepositional

    assert prepositional("Сургут") == "Сургуте"
    assert prepositional("Москва") == "Москве"
    assert prepositional("Пермь") == "Перми"
    assert prepositional("Сочи") == "Сочи"
    assert prepositional("Нижний Новгород") == "Нижний Новгороде"


WTTR_TEXT = "Пасмурно|+4°C|+-0°C|67%|→17km/h"


async def test_wttr_is_used_when_open_meteo_is_blocked(config) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "wttr.in" in str(request.url):
            return httpx.Response(200, text=WTTR_TEXT)
        raise httpx.ConnectTimeout("blocked", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WeatherService(config, client=client)
        result = await service.forecast("Сургут")

    assert result.ok is True
    assert "Сургуте" in result.speech
    assert "4" in result.speech
    assert "пасмурно" in result.speech
    assert "ветер 5 метров в секунду" in result.speech
    assert result.payload["source"] == "wttr.in"
    assert any("wttr.in" in str(r.url) for r in requests)


async def test_both_sources_failing_is_no_connection(config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("blocked", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WeatherService(config, client=client)
        result = await service.forecast("Сургут")

    assert result.ok is False
    assert result.error == "no_connection"
