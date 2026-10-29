"""Runtime orchestration tests with a fake voice pipeline, fake tools and fake LLM.

No audio, no network, no real user profile.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from voiceruna import runtime as runtime_module
from voiceruna.config import ConfigManager
from voiceruna.events import ANSWER, STATE, TOOL_CALL, TRANSCRIPT
from voiceruna.state import IDLE, PAUSED, THINKING, StateManager
from voiceruna.tools.base import ToolResult


class FakeVoice:
    """Stand-in for VoicePipeline: records speech, never opens a device."""

    instances: list["FakeVoice"] = []

    def __init__(self, config=None, state=None, emit=None, on_utterance=None, **kwargs) -> None:
        self.config = config
        self.state = state
        self.emit = emit
        self.on_utterance = on_utterance
        self.spoken: list[str] = []
        self.calls: list[str] = []
        self.paused = False
        self.preview_result: tuple[bool, str] = (True, "прозвучало")
        self.test_output_result: tuple[bool, str] = (True, "проверка звука прошла")
        self.test_input_result: tuple[bool, str, float] = (True, "уровень 12%", 0.12)
        self.test_stt_result: tuple[bool, str, str] = (True, "распознано", "открой блокнот")
        self.record_test_result: dict = {
            "ok": True,
            "text": "открой блокнот",
            "confidence": 0.85,
            "processing_s": 1.2,
            "wav_path": "C:/tmp/debug_mic.wav",
            "message": "распознано",
            "vad": {"start_s": 0.4, "end_s": 2.1, "speech_ms": 1700.0},
            "mic": {"device": "Микрофон (USB)", "samplerate": 16000},
        }
        FakeVoice.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def pause(self) -> None:
        self.paused = True
        self.calls.append("pause")

    async def resume(self) -> None:
        self.paused = False
        self.calls.append("resume")

    async def listen_now(self) -> None:
        self.calls.append("listen_now")

    async def speak(self, text: str) -> None:
        self.spoken.append(text)
        self.calls.append("speak")

    async def stop_speaking(self) -> None:
        self.calls.append("stop_speaking")

    async def preview(self, voice: str, text: str) -> tuple[bool, str]:
        self.calls.append("preview")
        return self.preview_result

    async def list_voices(self) -> list[dict[str, str]]:
        return []

    async def list_devices(self) -> dict:
        self.calls.append("list_devices")
        return {
            "input": [{"index": 1, "name": "Микрофон (USB)", "channels": 2, "hostapi": "MME", "default": True}],
            "output": [],
        }

    async def test_output(self, device=None) -> tuple[bool, str]:
        self.calls.append("test_output")
        return self.test_output_result

    async def test_input(self, device=None, seconds: float = 2.0) -> tuple[bool, str, float]:
        self.calls.append("test_input")
        return self.test_input_result

    async def test_stt(self) -> tuple[bool, str, str]:
        self.calls.append("test_stt")
        return self.test_stt_result

    async def record_and_recognize(self, seconds: float = 3.0) -> dict:
        self.calls.append("record_and_recognize")
        return self.record_test_result

    def diagnostics(self) -> dict:
        return {"state": "idle", "stt": {"ready": True}, "mock": True}

    def playback_status(self) -> dict:
        return {"ok": self.test_output_result[0], "backend": "mock", "message": self.test_output_result[1]}

    def apply_config(self) -> None:
        self.calls.append("apply_config")


class FakeTools:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.result = result or ToolResult(ok=True, speech="готово")
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, args))
        return self.result

    def specs(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]


class FakeLLM:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return self.result

    async def aclose(self) -> None:
        pass

    async def list_models(self, provider_id: str) -> list[str]:
        return []

    async def test_provider(self, provider_id: str) -> tuple[bool, str]:
        return True, "ok"

    def reset(self) -> None:
        self.calls.append({"reset": True})

    def disabled_ids(self) -> list[str]:
        return []


def make_llm_result(**kwargs):
    from voiceruna.llm import LLMResult

    return LLMResult(**kwargs)


@pytest.fixture()
def harness(monkeypatch, config, events):
    monkeypatch.setattr(runtime_module, "VoicePipeline", FakeVoice)
    state = StateManager()
    rt = runtime_module.AssistantRuntime(config, state, events)
    return rt, state


@pytest.mark.asyncio
async def test_fast_path_answers_without_llm(harness, events):
    rt, state = harness
    rt._tools = FakeTools(ToolResult(ok=True, speech="Открываю Блокнот"))
    rt._llm = FakeLLM(make_llm_result(text="should not be used"))

    answer = await rt.handle_text("Вокс, открой блокнот", source="window")

    assert answer == "Открываю Блокнот"
    assert rt._llm.calls == []
    assert rt._tools.calls == [("launch_application", {"application_id": "notepad"})]
    kinds = [event.type for event in events.collected]
    assert TRANSCRIPT in kinds and TOOL_CALL in kinds and ANSWER in kinds


@pytest.mark.asyncio
async def test_llm_tool_call_is_executed(harness, events):
    rt, _ = harness
    rt._tools = FakeTools(ToolResult(ok=True, speech="Открываю Загрузки"))
    rt._llm = FakeLLM(
        make_llm_result(
            tool_calls=[
                {"name": "open_folder", "args": {"folder_id": "downloads"}},
                {"name": "get_time", "args": {}},
            ]
        )
    )

    answer = await rt.handle_text("покажи мои загрузки пожалуйста", source="window")

    assert answer == "Открываю Загрузки"
    assert rt._tools.calls == [("open_folder", {"folder_id": "downloads"})]
    assert rt._llm.calls[0]["tools"]


@pytest.mark.asyncio
async def test_not_understood_phrase_from_config(harness, config):
    rt, _ = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(make_llm_result(kind="not_understood", text="NOT_UNDERSTOOD"))

    answer = await rt.handle_text("поставь будильник на семь", source="window")

    assert answer == config.get("errors.not_understood_phrase")


@pytest.mark.asyncio
async def test_no_connection_phrase_when_cascade_fails(harness, config):
    rt, _ = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(make_llm_result(error="no_connection"))

    answer = await rt.handle_text("сколько весит земля", source="window")

    assert answer == config.get("errors.no_connection_phrase")


@pytest.mark.asyncio
async def test_history_keeps_window_and_resets(harness, config):
    rt, _ = harness
    rt._tools = FakeTools(ToolResult(ok=True, speech="Сейчас 10:00"))
    rt._llm = FakeLLM(make_llm_result(text="ок"))
    config.set("llm.max_messages", 4, persist=False)
    rt._history = __import__("collections").deque(maxlen=4)

    for _ in range(3):
        await rt.handle_text("Вокс, который час", source="window")

    assert len(rt.history()) == 4  # two turns, window of four messages
    assert rt._reset_task is not None and not rt._reset_task.done()

    # the reset timer body is exercised directly: after 120 s of silence the window is dropped
    await rt._reset_history_after(0)
    assert rt.history() == []


@pytest.mark.asyncio
async def test_repeat_reruns_last_utterance(harness, events):
    rt, _ = harness
    rt._tools = FakeTools(ToolResult(ok=True, speech="Открываю Dota 2"))
    rt._llm = FakeLLM(make_llm_result(text="ок"))

    await rt.handle_text("Вокс, открой доту", source="voice")
    answer = await rt.repeat()

    assert answer == "Открываю Dota 2"
    assert rt._tools.calls[-1] == ("launch_application", {"application_id": "dota2"})
    assert any(event.data.get("replaced_transcript") == "Вокс, открой доту" for event in events.collected if event.type == ANSWER)


@pytest.mark.asyncio
async def test_pause_and_resume_drive_voice_and_state(harness):
    rt, state = harness
    voice = rt._voice

    await rt.set_paused(True)
    assert state.state == PAUSED
    assert voice.paused is True

    await rt.set_paused(False)
    assert state.state == IDLE
    assert voice.paused is False


@pytest.mark.asyncio
async def test_empty_text_is_ignored(harness):
    rt, state = harness
    rt._tools = FakeTools()

    assert await rt.handle_text("   ", source="window") == ""
    assert rt._voice.spoken == []


@pytest.mark.asyncio
async def test_missing_api_key_is_reported_as_setup_problem(harness, config, events):
    from voiceruna.llm import LLMResult

    rt, _ = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(LLMResult(error="no_api_key", detail="нет ключа: OPENROUTER_API_KEY (openrouter)"))

    answer = await rt.handle_text("сколько весит земля", source="window")

    assert answer == config.get("errors.no_api_key_phrase")
    assert answer != config.get("errors.no_connection_phrase")
    error = [e for e in events.collected if e.type == "error"][-1]
    assert error.data["kind"] == "no_api_key"
    assert "OPENROUTER_API_KEY" in error.data["message"]


@pytest.mark.asyncio
async def test_rejected_key_is_reported_separately(harness, config, events):
    from voiceruna.llm import LLMResult

    rt, _ = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(LLMResult(error="unauthorized"))

    answer = await rt.handle_text("привет", source="window")

    assert answer == config.get("errors.auth_failed_phrase")
    assert [e for e in events.collected if e.type == "error"][-1].data["kind"] == "auth"


@pytest.mark.asyncio
async def test_network_failure_still_says_no_connection(harness, config):
    from voiceruna.llm import LLMResult

    rt, _ = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(LLMResult(error="no_connection"))

    assert await rt.handle_text("привет", source="window") == config.get("errors.no_connection_phrase")


class RaisingLLM(FakeLLM):
    async def complete(self, messages, tools=None):
        raise RuntimeError("provider exploded")


@pytest.mark.asyncio
async def test_failed_turn_restores_idle_state(harness):
    """A crashing provider must not leave the assistant stuck in THINKING."""
    rt, state = harness
    rt._tools = FakeTools(ToolResult(ok=False, speech="не могу это сделать", error="x"))
    rt._llm = RaisingLLM()

    try:
        await rt.handle_text("расскажи анекдот", source="window")
    except Exception:
        pass

    assert state.state == IDLE
