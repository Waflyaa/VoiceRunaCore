"""Template edge cases for the fast path (TZ §6)."""

from __future__ import annotations

import pytest

from voiceruna.router import Router


@pytest.fixture()
def router(config, apps) -> Router:
    return Router(config, apps)


@pytest.mark.parametrize(
    "phrase",
    [
        "Вокс, который час",
        "Вокс, сколько времени",
        "Вокс, сколько сейчас времени",
        "Вокс, который сейчас час",
        "Вокс, время",
        "Вокс, текущее время",
        "скажи который час",
        "подскажи время",
    ],
)
def test_time_templates(router: Router, phrase: str) -> None:
    result = router.route(phrase)

    assert result.tool == "get_time"
    assert result.kind == "time"
    assert result.args == {}


@pytest.mark.parametrize(
    ("phrase", "folder_id"),
    [
        ("Вокс, открой папку загрузки", "downloads"),
        ("Вокс, открой папку Загрузки!", "downloads"),
        ("Вокс, открой папку документы", "documents"),
        ("Вокс, открой папку рабочий стол", "desktop"),
        ("Вокс, покажи папку загрузки", "downloads"),
        ("ВОКС, ОТКРОЙ ПАПКУ ДОКУМЕНТЫ", "documents"),
        ("папку загрузки", "downloads"),
    ],
)
def test_folder_templates(router: Router, phrase: str, folder_id: str) -> None:
    result = router.route(phrase)

    assert result.tool == "open_folder"
    assert result.kind == "folder"
    assert result.args == {"folder_id": folder_id}


@pytest.mark.parametrize(
    ("phrase", "folder_id", "filename"),
    [
        ("Вокс, открой файл отчёт.pdf в документах", "documents", "отчёт.pdf"),
        ("Вокс, файл смета.xlsx из загрузок", "downloads", "смета.xlsx"),
        ("Вокс, открой файл заметка.txt в папке документы", "documents", "заметка.txt"),
        ("открой файл фото.jpg на рабочем столе", "desktop", "фото.jpg"),
    ],
)
def test_file_templates(router: Router, phrase: str, folder_id: str, filename: str) -> None:
    result = router.route(phrase)

    assert result.tool == "open_file"
    assert result.kind == "file"
    assert result.args == {"folder_id": folder_id, "filename": filename}


@pytest.mark.parametrize(
    "phrase",
    [
        "Вокс, открой файл отчёт.pdf",
        "Вокс, открой файл отчёт.pdf в неведомой папке",
        "Вокс, открой папку неведомое",
    ],
)
def test_unparsed_templates_go_to_llm(router: Router, phrase: str) -> None:
    assert router.route(phrase).tool is None


@pytest.mark.parametrize(
    ("phrase", "city"),
    [
        ("Вокс, какая погода в Москве", "москве"),
        ("Вокс, какая сегодня погода", None),
        ("Вокс, погода", None),
        ("Вокс, какая погода в санкт петербурге", "санкт петербурге"),
        ("Вокс, погода в москве сейчас", "москве"),
        ("Вокс, погода на сегодня", None),
    ],
)
def test_weather_templates(router: Router, phrase: str, city: str | None) -> None:
    result = router.route(phrase)

    assert result.tool == "get_weather"
    assert result.kind == "weather"
    if city is None:
        assert result.args == {}
    else:
        assert result.args == {"city": city}


def test_commands_cannot_bypass_templates(router: Router) -> None:
    # A command word is stripped, the rest still has to parse.
    assert router.route("Вокс, запусти").tool is None
    assert router.route("Вокс, открой").tool is None
