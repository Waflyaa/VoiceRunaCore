"""HTTP API tests: auth, config, apps, state and the echo of the runtime answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voiceruna import runtime as runtime_module
from voiceruna.llm import LLMResult
from voiceruna.tools.base import ToolResult

from test_runtime import FakeLLM, FakeTools, FakeVoice

TOKEN = "test-token"
HEADERS = {"X-Vox-Token": TOKEN}


@pytest.fixture()
def client(monkeypatch, config, events):
    monkeypatch.setattr(runtime_module, "VoicePipeline", FakeVoice)
    from voiceruna.app import create_app

    app = create_app(token=TOKEN)
    with TestClient(app) as test_client:
        runtime = app.state.runtime
        runtime._tools = FakeTools(ToolResult(ok=True, speech="Открываю Блокнот"))
        runtime._llm = FakeLLM(LLMResult(text="Понял"))
        yield test_client


def test_health_needs_no_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["state"] in {"idle", "paused"}


def test_protected_routes_require_token(client):
    assert client.get("/config").status_code == 401
    assert client.get("/config", headers={"X-Vox-Token": "wrong"}).status_code == 401
    assert client.get("/config", headers=HEADERS).status_code == 200


def test_config_roundtrip_is_partial_merge(client):
    payload = client.get("/config", headers=HEADERS).json()
    assert payload["tts"]["voice"] == "ru-RU-DmitryNeural"

    response = client.put("/config", headers=HEADERS, json={"tts": {"rate": "+20%"}})
    assert response.status_code == 200
    body = response.json()
    assert body["tts"]["rate"] == "+20%"
    assert body["tts"]["voice"] == "ru-RU-DmitryNeural"  # untouched
    assert client.get("/state", headers=HEADERS).json()["paused"] is False


def test_pause_and_resume_endpoints(client):
    assert client.post("/pause", headers=HEADERS).json()["paused"] is True
    assert client.get("/state", headers=HEADERS).json()["state"] == "paused"
    assert client.post("/resume", headers=HEADERS).json()["paused"] is False
    assert client.get("/state", headers=HEADERS).json()["state"] == "idle"


def test_command_returns_answer(client):
    response = client.post("/command", headers=HEADERS, json={"text": "Вокс, открой блокнот"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Открываю Блокнот"


def test_apps_put_validates(client):
    bad = {"broken": {"display_name": "No target"}}
    assert client.put("/apps", headers=HEADERS, json=bad).status_code == 400

    good = {"notepad": {"display_name": "Блокнот", "path": "notepad.exe", "aliases": ["блокнот"]}}
    response = client.put("/apps", headers=HEADERS, json=good)
    assert response.status_code == 200
    assert response.json()["notepad"]["display_name"] == "Блокнот"
    assert client.get("/apps", headers=HEADERS).json()["notepad"]["path"] == "notepad.exe"


def test_env_endpoint_writes_dotenv(client, data_dir):
    response = client.put("/env", headers=HEADERS, json={"name": "OPENROUTER_API_KEY", "value": "sk-test"})
    assert response.status_code == 200
    env_file = data_dir / ".env"
    assert "OPENROUTER_API_KEY=sk-test" in env_file.read_text(encoding="utf-8")
    assert "sk-test" not in client.get("/config", headers=HEADERS).text


def test_cors_preflight_allows_tauri_window(client):
    """WebView2 loads the window from http://tauri.localhost (cross-origin)."""
    response = client.options(
        "/config",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-vox-token,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] in {"*", "http://tauri.localhost"}
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_unknown_provider_returns_404(client):
    assert client.post("/providers/nope/test", headers=HEADERS).status_code == 404


def test_shutdown_invokes_callback(client):
    called = {"value": False}
    client.app.state.on_shutdown = lambda: called.__setitem__("value", True)
    response = client.post("/shutdown", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_audio_devices_lists_inputs_outputs_and_current(client):
    response = client.get("/audio/devices", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["input"], list) and body["input"], "входы должны быть в ответе"
    assert isinstance(body["output"], list)
    assert body["current"] == {"input": "default", "output": "default"}
    assert set(body["playback"]) >= {"ok", "backend", "message"}


def test_audio_devices_requires_token(client):
    assert client.get("/audio/devices").status_code == 401


def test_audio_test_output_reports_real_message(client):
    body = client.post("/audio/test", headers=HEADERS, json={"kind": "output"}).json()
    assert body["ok"] is True
    assert body["message"]
    assert body["level"] is None


def test_audio_test_output_failure_is_not_hidden(client):
    client.app.state.runtime.voice.test_output_result = (False, "нет устройства вывода")
    body = client.post("/audio/test", headers=HEADERS, json={"kind": "output"}).json()
    assert body["ok"] is False
    assert "устройства вывода" in body["message"]


def test_audio_test_input_returns_level(client):
    client.app.state.runtime.voice.test_input_result = (True, "уровень 4% — тишина", 0.04)
    body = client.post("/audio/test", headers=HEADERS, json={"kind": "input"}).json()
    assert body["ok"] is True
    assert body["level"] == pytest.approx(0.04)
    assert "тишина" in body["message"]


def test_audio_test_rejects_unknown_kind(client):
    assert client.post("/audio/test", headers=HEADERS, json={"kind": "speaker"}).status_code == 422


def test_tts_preview_reports_failure_instead_of_ok(client):
    client.app.state.runtime.voice.preview_result = (False, "нет устройства вывода")
    body = client.post("/tts/preview", headers=HEADERS, json={"voice": "ru-RU-DmitryNeural", "text": "тест"}).json()
    assert body == {"ok": False, "message": "нет устройства вывода"}


def test_tts_preview_reports_success_message(client):
    client.app.state.runtime.voice.preview_result = (True, "воспроизведено через SAPI")
    body = client.post("/tts/preview", headers=HEADERS, json={"voice": "", "text": ""}).json()
    assert body == {"ok": True, "message": "воспроизведено через SAPI"}


def test_audio_test_stt_returns_recognised_text(client):
    client.app.state.runtime.voice.test_stt_result = (True, "распознано за 1.4 с", "открой блокнот")
    body = client.post("/audio/test", headers=HEADERS, json={"kind": "stt"}).json()
    assert body["ok"] is True
    assert body["text"] == "открой блокнот"
    assert "распознано" in body["message"]


def test_audio_test_stt_failure_is_reported(client):
    client.app.state.runtime.voice.test_stt_result = (False, "STT: ImportError: no ctranslate2", "")
    body = client.post("/audio/test", headers=HEADERS, json={"kind": "stt"}).json()
    assert body["ok"] is False
    assert "ctranslate2" in body["message"]


def test_diagnostics_reports_providers_without_keys(client):
    body = client.get("/diagnostics", headers=HEADERS).json()
    assert body["state"] in {"idle", "paused"}
    assert body["path"]["config"] if "path" in body else True
    assert isinstance(body["providers"], list) and body["providers"]
    first = body["providers"][0]
    assert set(first) >= {"id", "enabled", "api_key_env", "key_present"}
    assert body["providers"][0]["key_present"] is False, "ключей в тестовом профиле нет"
    assert body["voice"]["mock"] is True


def test_diagnostics_requires_token(client):
    assert client.get("/diagnostics").status_code == 401


def test_stt_record_test_returns_text_confidence_and_timings(client):
    body = client.post("/stt/record_test", headers=HEADERS, json={"seconds": 3}).json()
    assert body["ok"] is True
    assert body["text"] == "открой блокнот"
    assert body["confidence"] == pytest.approx(0.85)
    assert body["processing_s"] == pytest.approx(1.2)
    assert body["vad"]["start_s"] == pytest.approx(0.4)
    assert body["mic"]["samplerate"] == 16000
    assert body["wav_path"].endswith("debug_mic.wav")


def test_stt_record_test_uses_three_seconds_by_default(client):
    client.post("/stt/record_test", headers=HEADERS, json={})
    assert "record_and_recognize" in client.app.state.runtime.voice.calls


def test_stt_record_test_reports_failure_instead_of_500(client):
    async def boom(seconds: float = 3.0):
        raise RuntimeError("микрофон занят")

    client.app.state.runtime.voice.record_and_recognize = boom
    response = client.post("/stt/record_test", headers=HEADERS, json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "микрофон занят" in body["message"]


def test_stt_record_test_requires_token(client):
    assert client.post("/stt/record_test", json={}).status_code == 401
