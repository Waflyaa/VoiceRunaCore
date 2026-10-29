"""Tests for the fast-path router (TZ §6, live phrases from §2.1)."""

from __future__ import annotations

import pytest

from voiceruna.router import RouteResult, Router
from voiceruna.router.normalize import normalize


@pytest.fixture()
def router(config, apps) -> Router:
    return Router(config, apps)


LIVE_PHRASES = [
    ("Вокс, открой доту", "launch_application", {"application_id": "dota2"}),
    ("Вокс, запусти Factorio", "launch_application", {"application_id": "factorio"}),
    ("Вокс, открой Brave", "launch_application", {"application_id": "brave"}),
    ("Вокс, открой блокнот", "launch_application", {"application_id": "notepad"}),
    ("Вокс, запусти калькулятор", "launch_application", {"application_id": "calc"}),
    ("Вокс, открой дискорд", "launch_application", {"application_id": "discord"}),
    ("Вокс, открой телеграм", "launch_application", {"application_id": "telegram"}),
    ("Вокс, открой проводник", "launch_application", {"application_id": "explorer"}),
    ("Вокс, открой стим", "launch_application", {"application_id": "steam"}),
    ("Вокс, открой вс код", "launch_application", {"application_id": "vscode"}),
    ("Вокс, открой папку Загрузки", "open_folder", {"folder_id": "downloads"}),
    ("Вокс, который час", "get_time", {}),
]


@pytest.mark.parametrize(("phrase", "tool", "args"), LIVE_PHRASES)
def test_live_phrases(router: Router, phrase: str, tool: str, args: dict) -> None:
    result = router.route(phrase)

    assert result.tool == tool
    assert result.args == args


def test_weather_phrases(router: Router) -> None:
    default = router.route("Вокс, какая сегодня погода")
    assert default.tool == "get_weather"
    assert default.args == {}

    named = router.route("Вокс, какая погода в Москве")
    assert named.tool == "get_weather"
    assert named.args["city"] == "москве"


def test_kinds(router: Router) -> None:
    assert router.route("Вокс, открой доту").kind == "alias"
    assert router.route("Вокс, открой дот").kind == "fuzzy"
    assert router.route("Вокс, который час").kind == "time"
    assert router.route("Вокс, какая сегодня погода").kind == "weather"
    assert router.route("Вокс, открой папку Загрузки").kind == "folder"
    assert router.route("Вокс, открой файл отчёт.pdf в документах").kind == "file"
    assert router.route("просто поболтать").kind == "none"


def test_small_talk_goes_to_llm(router: Router) -> None:
    assert router.route("Вокс, сколько весит Земля").tool is None
    assert router.route("просто поболтать").tool is None
    assert router.route("расскажи про NAT").tool is None


def test_case_and_punctuation(router: Router) -> None:
    assert router.route("ВОКС, ОТКРОЙ ДОТУ!!!").args == {"application_id": "dota2"}
    assert router.route("вокс открой вс код.").args == {"application_id": "vscode"}
    assert router.route("  Вокс,   открой   телеграм  ").args == {"application_id": "telegram"}


def test_yo_letter_is_normalized(router: Router) -> None:
    assert normalize("Вокс, ёлка") == "елка"
    assert normalize("ВОКС, ОТКРОЙ ЁЖА") == "ежа"


def test_yo_alias_matches_after_normalization(router: Router, apps) -> None:
    apps.replace({"hedgehog": {"display_name": "Ёж", "path": "eж.exe", "aliases": ["ёж"]}})

    result = router.route("Вокс, открой ёж")

    assert result.tool == "launch_application"
    assert result.args == {"application_id": "hedgehog"}


def test_ambiguous_fuzzy_goes_to_llm(router: Router, apps) -> None:
    apps.replace(
        {
            "picture": {"display_name": "Картина", "path": "a.exe", "aliases": ["картина"]},
            "pictures": {"display_name": "Картины", "path": "b.exe", "aliases": ["картины"]},
        }
    )

    result = router.route("Вокс, открой картине")

    assert result.tool is None
    assert result.kind == "none"


def test_threshold_comes_from_config(router: Router, config) -> None:
    assert router.route("Вокс, открой дот").tool == "launch_application"

    config.set("router.fuzzy_threshold", 0.99)

    assert router.route("Вокс, открой дот").tool is None


def test_empty_and_non_string_input(router: Router) -> None:
    assert router.route("").tool is None
    assert router.route("Вокс").tool is None
    assert router.route("Вокс, открой").tool is None
    assert router.route(None).tool is None  # type: ignore[arg-type]
    assert router.route(123).tool is None  # type: ignore[arg-type]


def test_route_result_shape(router: Router) -> None:
    result = router.route("Вокс, открой доту")

    assert isinstance(result, RouteResult)
    assert result.tool == "launch_application"
    assert isinstance(result.args, dict)
    assert result.kind in {"alias", "fuzzy", "time", "weather", "folder", "file", "none"}


def test_package_reexports() -> None:
    from voiceruna.router import RouteResult as PackageResult
    from voiceruna.router import Router as PackageRouter
    from voiceruna.router.engine import RouteResult as EngineResult
    from voiceruna.router.engine import Router as EngineRouter

    assert PackageRouter is EngineRouter
    assert PackageResult is EngineResult
