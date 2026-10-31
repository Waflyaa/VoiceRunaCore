"""Abilities markdown generation and the "what can you do" intent."""

from __future__ import annotations

import json

import pytest

from voiceruna import __version__, abilities
from voiceruna.events import ANSWER


@pytest.fixture()
def harness(monkeypatch, config, events):
    """AssistantRuntime with the fake voice pipeline (same as test_runtime)."""
    from voiceruna import runtime as runtime_module
    from voiceruna.state import StateManager
    from test_runtime import FakeVoice

    monkeypatch.setattr(runtime_module, "VoicePipeline", FakeVoice)
    state = StateManager()
    rt = runtime_module.AssistantRuntime(config, state, events)
    return rt, state


def test_abilities_files_are_created_in_both_languages(tmp_path):
    ru_path, en_path = abilities.ensure_files(tmp_path)

    assert ru_path.name == "Abilities_RU.md"
    assert en_path.name == "Abilities_ENG.md"
    ru = ru_path.read_text(encoding="utf-8")
    en = en_path.read_text(encoding="utf-8")
    assert "# Возможности VoiceRunaCore" in ru
    assert "# VoiceRunaCore abilities" in en
    assert __version__ in ru and __version__ in en
    assert "громкость" in ru.lower() and "volume" in en.lower()


def test_abilities_files_are_refreshed_on_every_start(tmp_path):
    ru_path, _ = abilities.ensure_files(tmp_path)
    ru_path.write_text("stale", encoding="utf-8")

    abilities.ensure_files(tmp_path)

    assert "stale" not in ru_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_abilities_intent_returns_a_card_not_a_lecture(harness, events, data_dir):
    rt, _state = harness

    answer = await rt.handle_text("Вокс, что ты умеешь?", source="voice")

    assert answer == "Я скинул список своих возможностей в чат"
    answers = [event for event in events.collected if event.type == ANSWER]
    assert answers and answers[-1].data.get("action") == "open_abilities"
    assert (data_dir / "Abilities_RU.md").is_file()
    assert (data_dir / "Abilities_ENG.md").is_file()
    # No LLM call for this intent.
    assert getattr(rt._llm, "calls", []) == [] or rt._llm is None


@pytest.mark.asyncio
async def test_abilities_intent_does_not_fire_for_regular_questions(harness, events):
    from test_runtime import FakeLLM, FakeTools, make_llm_result

    rt, _state = harness
    rt._tools = FakeTools()
    rt._llm = FakeLLM(make_llm_result(text="Столица Франции — Париж."))

    answer = await rt.handle_text("какая столица Франции", source="voice")

    assert answer == "Столица Франции — Париж."
    answers = [event for event in events.collected if event.type == ANSWER]
    assert answers and "action" not in answers[-1].data
