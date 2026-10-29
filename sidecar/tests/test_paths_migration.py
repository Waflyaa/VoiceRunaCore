"""First-run migration from the legacy %APPDATA%\\VoxRunaCore folder."""

from __future__ import annotations

from voiceruna import paths


def _legacy(tmp_path):
    legacy = tmp_path / "VoxRunaCore"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"config_version": 2}', encoding="utf-8")
    (legacy / ".env").write_text("DEEPSEEK_API_KEY=test\n", encoding="utf-8")
    (legacy / "apps.json").write_text("{}", encoding="utf-8")
    (legacy / "models").mkdir()
    (legacy / "models" / "model.bin").write_bytes(b"weights")
    return legacy


def test_legacy_folder_is_migrated_once(tmp_path, monkeypatch):
    monkeypatch.delenv("VOX_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    legacy = _legacy(tmp_path)

    target = paths.data_dir()

    assert target == tmp_path / "VoiceRunaCore"
    assert (target / "config.json").is_file()
    assert (target / ".env").read_text(encoding="utf-8").startswith("DEEPSEEK_API_KEY=")
    assert (target / "apps.json").is_file()
    assert (target / "models" / "model.bin").read_bytes() == b"weights"
    assert not legacy.exists()


def test_existing_target_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.delenv("VOX_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    _legacy(tmp_path)
    target = tmp_path / "VoiceRunaCore"
    target.mkdir()
    (target / "config.json").write_text('{"mine": true}', encoding="utf-8")

    paths.data_dir()

    assert (target / "config.json").read_text(encoding="utf-8") == '{"mine": true}'
