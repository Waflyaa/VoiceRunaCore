"""Tests for the apps.json registry (TZ §9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voiceruna.apps import DEFAULT_APPS, AppsRegistry

EXPECTED_IDS = [
    "dota2",
    "factorio",
    "steam",
    "discord",
    "telegram",
    "vscode",
    "brave",
    "explorer",
    "notepad",
    "calc",
]


def test_template_is_created_on_first_run(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    registry = AppsRegistry(path)

    data = registry.load()

    assert path.exists()
    assert list(data) == EXPECTED_IDS
    assert data == DEFAULT_APPS
    assert list(tmp_path.glob("*.tmp")) == []


def test_default_template_matches_tz_section_9() -> None:
    assert DEFAULT_APPS["dota2"]["uri"] == "steam://rungameid/570"
    assert DEFAULT_APPS["factorio"]["uri"] == "steam://rungameid/427520"
    assert DEFAULT_APPS["notepad"]["display_name"] == "Блокнот"
    assert DEFAULT_APPS["notepad"]["path"] == "notepad.exe"
    assert DEFAULT_APPS["calc"]["path"] == "calc.exe"
    assert DEFAULT_APPS["explorer"]["path"] == "explorer.exe"
    assert DEFAULT_APPS["vscode"]["display_name"] == "VS Code"
    assert DEFAULT_APPS["discord"]["path"].endswith("--processStart Discord.exe")
    assert set(DEFAULT_APPS["dota2"]["aliases"]) == {"дота", "доту", "доту 2", "dota", "dota2"}


def test_load_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    registry = AppsRegistry(path)

    first = registry.load()
    second = registry.load()

    assert first == second
    assert registry.ids() == EXPECTED_IDS
    assert path.read_text(encoding="utf-8").startswith("{")


def test_load_uses_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    custom = {"music": {"display_name": "Музыка", "path": r"C:\Music\player.exe", "aliases": ["музыка"]}}
    path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")

    registry = AppsRegistry(path)

    assert registry.load() == custom


def test_load_corrupt_file_falls_back_to_template(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    path.write_text("not json at all", encoding="utf-8")

    registry = AppsRegistry(path)

    assert registry.load() == DEFAULT_APPS
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_APPS


def test_load_skips_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    data = {
        "good": {"display_name": "Good", "path": "good.exe", "aliases": ["гуд"]},
        "bad": {"display_name": "Bad"},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    registry = AppsRegistry(path)
    registry.load()

    assert registry.ids() == ["good"]


def test_replace_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    registry = AppsRegistry(path)
    registry.load()
    new_data = {"music": {"display_name": "Музыка", "path": r"C:\Music\player.exe", "aliases": ["музыка"]}}

    replaced = registry.replace(new_data)

    assert replaced == new_data
    assert json.loads(path.read_text(encoding="utf-8")) == new_data
    assert AppsRegistry(path).load() == new_data
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "data",
    [
        {"bad": {"path": "x.exe"}},
        {"bad": {"display_name": "X"}},
        {"bad": {"display_name": "X", "path": "x.exe", "uri": "steam://1"}},
        {"bad": {"display_name": "", "path": "x.exe"}},
        {"bad": {"display_name": "X", "path": ""}},
        {"bad": {"display_name": "X", "uri": "   "}},
        {"bad": {"display_name": "X", "path": "x.exe", "aliases": "алиас"}},
        {"bad": {"display_name": "X", "path": "x.exe", "aliases": [1]}},
        {"Bad Id": {"display_name": "X", "path": "x.exe"}},
        {"": {"display_name": "X", "path": "x.exe"}},
        {"bad!": {"display_name": "X", "path": "x.exe"}},
        {"bad": "not a dict"},
        ["not", "a", "dict"],
    ],
)
def test_replace_rejects_invalid_data(tmp_path: Path, data: object) -> None:
    registry = AppsRegistry(tmp_path / "apps.json")
    registry.load()

    with pytest.raises(ValueError):
        registry.replace(data)  # type: ignore[arg-type]


def test_replace_does_not_persist_invalid_data(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    registry = AppsRegistry(path)
    registry.load()
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        registry.replace({"bad": {"display_name": "X"}})

    assert path.read_text(encoding="utf-8") == before
    assert registry.ids() == EXPECTED_IDS


def test_get_returns_a_copy(tmp_path: Path) -> None:
    registry = AppsRegistry(tmp_path / "apps.json")
    registry.load()

    entry = registry.get("dota2")
    assert entry is not None
    entry["display_name"] = "Hacked"

    assert registry.get("dota2")["display_name"] == "Dota 2"
    assert registry.get("unknown") is None


def test_aliases_are_normalized(tmp_path: Path) -> None:
    registry = AppsRegistry(tmp_path / "apps.json")
    registry.load()
    registry.replace(
        {
            "music": {
                "display_name": "Музыка",
                "path": "player.exe",
                "aliases": ["Ёж", "  Дота   2 ", "МУЗЫКА"],
            }
        }
    )

    aliases = registry.aliases()

    assert aliases["еж"] == "music"
    assert aliases["дота 2"] == "music"
    assert aliases["музыка"] == "music"


def test_describe_for_prompt(tmp_path: Path) -> None:
    registry = AppsRegistry(tmp_path / "apps.json")
    registry.load()

    expected = ", ".join(
        f"{app_id} ({DEFAULT_APPS[app_id]['display_name']})" for app_id in EXPECTED_IDS
    )

    assert registry.describe_for_prompt() == expected


def test_describe_for_prompt_handles_empty_registry(tmp_path: Path) -> None:
    registry = AppsRegistry(tmp_path / "apps.json")
    registry.load()
    registry.replace({})

    assert registry.describe_for_prompt() == ""
    assert registry.ids() == []
    assert registry.aliases() == {}
