"""Tests for the five tools and ToolExecutor (TZ §8).

No real program is started: subprocess.Popen and os.startfile are replaced by
recorders, so the suite is safe to run on any machine.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from voiceruna import security
from voiceruna.tools import TOOL_NAMES, ToolExecutor, ToolResult, WeatherService


@pytest.fixture()
def executor(config, apps) -> ToolExecutor:
    return ToolExecutor(config, apps)


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    recorded: list[tuple] = []

    def fake_popen(command, **kwargs):
        recorded.append(("popen", list(command), kwargs))
        return object()

    def fake_startfile(target):
        recorded.append(("startfile", target))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "startfile", fake_startfile, raising=False)
    return recorded


# --------------------------------------------------------------------------- specs
def test_specs_are_exactly_the_five_tools(executor: ToolExecutor, apps) -> None:
    specs = executor.specs()

    assert [spec["function"]["name"] for spec in specs] == list(TOOL_NAMES)
    assert all(spec["type"] == "function" for spec in specs)

    launch = specs[0]["function"]["parameters"]
    assert launch["properties"]["application_id"]["enum"] == apps.ids()
    assert launch["required"] == ["application_id"]

    folder = specs[1]["function"]["parameters"]
    assert folder["properties"]["folder_id"]["enum"] == ["desktop", "downloads", "documents"]

    open_file = specs[2]["function"]["parameters"]
    assert sorted(open_file["required"]) == ["filename", "folder_id"]

    assert specs[3]["function"]["parameters"]["required"] == []

    weather = specs[4]["function"]["parameters"]
    assert weather["required"] == []
    assert weather["properties"]["city"]["maxLength"] == 100

    assert executor.app_ids() == apps.ids()


# --------------------------------------------------------------------------- refusals
async def test_unknown_tool_is_refused(executor: ToolExecutor, config) -> None:
    result = await executor.execute("delete_everything", {"path": "C:\\"})

    assert result.ok is False
    assert result.speech == config.get("errors.tool_refused_phrase")
    assert result.error


async def test_non_dict_arguments_are_refused(executor: ToolExecutor) -> None:
    assert (await executor.execute("get_time", "oops")).ok is False  # type: ignore[arg-type]
    assert (await executor.execute("open_folder", [1, 2])).ok is False  # type: ignore[arg-type]


async def test_none_arguments_are_safe(executor: ToolExecutor) -> None:
    result = await executor.execute("get_time", None)  # type: ignore[arg-type]

    assert result.ok is True
    assert re.fullmatch(r"Сейчас \d{2}:\d{2}", result.speech)


async def test_unknown_argument_is_refused(executor: ToolExecutor, calls: list[tuple]) -> None:
    result = await executor.execute("launch_application", {"application_id": "dota2", "path": "evil.exe"})

    assert result.ok is False
    assert calls == []


# --------------------------------------------------------------------------- launch
async def test_launch_uri_uses_startfile(executor: ToolExecutor, calls: list[tuple]) -> None:
    result = await executor.execute("launch_application", {"application_id": "dota2"})

    assert result.ok is True
    assert result.speech == "Открываю Dota 2"
    assert calls == [("startfile", "steam://rungameid/570")]


async def test_launch_rejects_disallowed_uri_scheme(
    executor: ToolExecutor, apps, calls: list[tuple]
) -> None:
    apps.replace({"evil": {"display_name": "Evil", "uri": "http://evil.example/x", "aliases": []}})

    result = await executor.execute("launch_application", {"application_id": "evil"})

    assert result.ok is False
    assert calls == []


async def test_launch_missing_path_says_not_found(
    executor: ToolExecutor, apps, calls: list[tuple], tmp_path: Path
) -> None:
    apps.replace(
        {
            "ghost": {
                "display_name": "Ghost",
                "path": str(tmp_path / "missing" / "ghost.exe"),
                "aliases": [],
            }
        }
    )

    result = await executor.execute("launch_application", {"application_id": "ghost"})

    assert result.ok is True
    assert result.speech == "Не нашёл Ghost"
    assert calls == []


async def test_launch_existing_path_uses_popen(
    executor: ToolExecutor, apps, calls: list[tuple], tmp_path: Path
) -> None:
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"")
    apps.replace({"app": {"display_name": "App", "path": str(executable), "aliases": []}})

    result = await executor.execute("launch_application", {"application_id": "app"})

    assert result.ok is True
    assert result.speech == "Открываю App"
    assert calls == [("popen", [str(executable)], {"shell": False})]


async def test_launch_bare_executable_uses_path(
    executor: ToolExecutor, apps, calls: list[tuple]
) -> None:
    apps.replace({"notepad": {"display_name": "Блокнот", "path": "notepad.exe", "aliases": []}})

    result = await executor.execute("launch_application", {"application_id": "notepad"})

    assert result.ok is True
    assert calls == [("popen", ["notepad.exe"], {"shell": False})]


async def test_launch_unknown_application_is_refused(executor: ToolExecutor) -> None:
    assert (await executor.execute("launch_application", {"application_id": "nope"})).ok is False
    assert (await executor.execute("launch_application", {})).ok is False


async def test_launch_os_error_is_a_refusal(
    executor: ToolExecutor, monkeypatch: pytest.MonkeyPatch, config
) -> None:
    def broken_startfile(target):
        raise OSError("cannot open")

    monkeypatch.setattr(os, "startfile", broken_startfile, raising=False)

    result = await executor.execute("launch_application", {"application_id": "dota2"})

    assert result.ok is False
    assert result.speech == config.get("errors.tool_refused_phrase")


# --------------------------------------------------------------------------- folders
async def test_open_folder(
    executor: ToolExecutor,
    calls: list[tuple],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(security, "known_folder", lambda folder_id: downloads)

    result = await executor.execute("open_folder", {"folder_id": "downloads"})

    assert result.ok is True
    assert result.speech == "Открываю Загрузки"
    assert calls == [("startfile", str(downloads))]


async def test_open_folder_rejects_unknown_folder(executor: ToolExecutor, calls: list[tuple]) -> None:
    result = await executor.execute("open_folder", {"folder_id": "windows"})

    assert result.ok is False
    assert calls == []


# --------------------------------------------------------------------------- files
async def test_open_file(
    executor: ToolExecutor,
    calls: list[tuple],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    document = desktop / "note.txt"
    document.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(security, "known_folder", lambda folder_id: desktop)

    result = await executor.execute("open_file", {"folder_id": "desktop", "filename": "note.txt"})

    assert result.ok is True
    assert result.speech == "Открываю note.txt"
    assert calls == [("startfile", str(document.resolve()))]


async def test_open_file_missing(executor: ToolExecutor, calls: list[tuple], monkeypatch, tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(security, "known_folder", lambda folder_id: desktop)

    result = await executor.execute("open_file", {"folder_id": "desktop", "filename": "note.txt"})

    assert result.ok is False
    assert result.speech == "Не нашёл note.txt"
    assert calls == []


async def test_open_file_rejects_bad_arguments(executor: ToolExecutor, calls: list[tuple]) -> None:
    assert (await executor.execute("open_file", {"folder_id": "desktop"})).ok is False
    assert (
        await executor.execute("open_file", {"folder_id": "desktop", "filename": "../secret.txt"})
    ).ok is False
    assert (
        await executor.execute("open_file", {"folder_id": "desktop", "filename": "app.exe"})
    ).ok is False
    assert calls == []


# --------------------------------------------------------------------------- time
async def test_get_time(executor: ToolExecutor) -> None:
    result = await executor.execute("get_time", {})

    assert result.ok is True
    assert re.fullmatch(r"Сейчас \d{2}:\d{2}", result.speech)
    assert re.fullmatch(r"\d{2}:\d{2}", result.payload["time"])


async def test_get_time_rejects_arguments(executor: ToolExecutor) -> None:
    assert (await executor.execute("get_time", {"city": "Москва"})).ok is False


# --------------------------------------------------------------------------- weather
async def test_get_weather_delegates_to_service(
    executor: ToolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    async def fake_forecast(self, city):
        seen.append(city)
        return ToolResult(ok=True, speech=f"погода: {city}")

    monkeypatch.setattr(WeatherService, "forecast", fake_forecast)

    result = await executor.execute("get_weather", {"city": "Москва"})
    default = await executor.execute("get_weather", {})

    assert result.speech == "погода: Москва"
    assert default.speech == "погода: None"
    assert seen == ["Москва", None]


async def test_get_weather_validates_arguments(executor: ToolExecutor) -> None:
    assert (await executor.execute("get_weather", {"city": 42})).ok is False
    assert (await executor.execute("get_weather", {"city": "а" * 101})).ok is False
    assert (await executor.execute("get_weather", {"city": "Москва", "days": 3})).ok is False


# --------------------------------------------------------------------------- exports
def test_package_reexports_contract_names() -> None:
    from voiceruna.tools import ToolExecutor as PackageExecutor
    from voiceruna.tools import ToolResult as PackageResult
    from voiceruna.tools import WeatherService as PackageWeather
    from voiceruna.tools.base import ToolResult as BaseResult
    from voiceruna.tools.executor import ToolExecutor as ModuleExecutor

    assert PackageExecutor is ModuleExecutor
    assert PackageResult is BaseResult
    assert PackageWeather is WeatherService
    assert TOOL_NAMES == (
        "launch_application",
        "open_folder",
        "open_file",
        "get_time",
        "get_weather",
    )
