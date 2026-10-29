"""One-time config migrations (v1 -> v2: offline SAPI fallback by default)."""

from __future__ import annotations

import json
from pathlib import Path

from voiceruna.config import ConfigManager


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_v1_config_with_edge_engine_is_upgraded_to_auto(tmp_path: Path):
    path = tmp_path / "config.json"
    _write(path, {"tts": {"engine": "edge"}, "llm": {"timeout_sec": 15}})

    manager = ConfigManager(path)
    data = manager.load()

    assert data["tts"]["engine"] == "auto"
    assert data["config_version"] == 2
    assert data["llm"]["timeout_sec"] == 15, "остальные значения не трогаем"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tts"]["engine"] == "auto"


def test_explicit_engine_choice_survives_the_migration(tmp_path: Path):
    path = tmp_path / "config.json"
    _write(path, {"config_version": 2, "tts": {"engine": "edge"}})

    manager = ConfigManager(path)
    data = manager.load()

    assert data["tts"]["engine"] == "edge"
    assert data["config_version"] == 2


def test_sapi_choice_is_never_migrated(tmp_path: Path):
    path = tmp_path / "config.json"
    _write(path, {"config_version": 1, "tts": {"engine": "sapi"}})

    manager = ConfigManager(path)
    data = manager.load()

    assert data["tts"]["engine"] == "sapi"
    assert data["config_version"] == 2


def test_mojibake_phrases_are_repaired_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    broken = "Не расслышал, повтори".encode("utf-8").decode("cp1251")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"errors": {"stt_failed_phrase": broken}}, ensure_ascii=False), encoding="utf-8")

    cfg = ConfigManager()
    cfg.load()

    assert cfg.get("errors.stt_failed_phrase") == "Не расслышал, повтори"
    assert "Не расслышал, повтори" in path.read_text(encoding="utf-8")


def test_double_mojibake_is_repaired(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    once = "Сургут".encode("utf-8").decode("cp1251")
    twice = once.encode("utf-8").decode("cp1251")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"weather": {"default_city": twice}}, ensure_ascii=False), encoding="utf-8")

    cfg = ConfigManager()
    cfg.load()

    assert cfg.get("weather.default_city") == "Сургут"


def test_clean_config_strings_are_not_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    payload = {
        "errors": {"stt_failed_phrase": "Не расслышал, повтори"},
        "llm": {"providers": [{"id": "deepseek", "base_url": "https://api.deepseek.com/v1"}]},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    cfg = ConfigManager()
    cfg.load()

    assert cfg.get("errors.stt_failed_phrase") == "Не расслышал, повтори"
    assert cfg.get("llm.providers")[0]["base_url"] == "https://api.deepseek.com/v1"


def test_per_device_calibration_survives_a_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "audio": {
                    "input_device": "index:7",
                    "calibration": {
                        "index:7": {"noise_dbfs": -58.7, "peak_dbfs": -40.1, "seconds": 3}
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = ConfigManager()
    cfg.load()

    table = cfg.get("audio.calibration")
    assert isinstance(table, dict)
    assert table["index:7"]["noise_dbfs"] == -58.7
    assert table["index:7"]["seconds"] == 3


def test_unknown_keys_are_still_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mystery": {"a": 1}, "audio": {"input_device": "default"}}),
        encoding="utf-8",
    )

    cfg = ConfigManager()
    cfg.load()

    assert cfg.get("mystery", None) is None
