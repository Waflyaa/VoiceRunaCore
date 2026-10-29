"""Wake detection without a keyword model: VAD capture + Whisper check."""

from __future__ import annotations

import asyncio

import pytest

from voiceruna.state import IDLE, LISTENING, SPEAKING

from test_voice_pipeline import SPEECH, SILENCE, Rig, push, quiet_config, until


# ---------------------------------------------------------------- vad_stt wake mode
def _wav_mode_rig(config, events, stt_text):
    rig = Rig(config, events, stt_text=stt_text, wake_mode="vad_stt")
    quiet_config(config)
    return rig


@pytest.mark.asyncio
async def test_vad_stt_dispatches_only_the_command_after_the_wake_word(config, events):
    rig = _wav_mode_rig(config, events, "Вокс, открой блокнот")
    await rig.pipeline.start()

    await push(rig.mic, [SILENCE] + [SPEECH] * 8 + [SILENCE] * 6)
    assert await until(lambda: rig.spoken), "команда должна уйти в runtime"
    assert rig.spoken[0] == "открой блокнот"
    await rig.pipeline.stop()


@pytest.mark.asyncio
async def test_vad_stt_ignores_phrases_without_the_wake_word(config, events):
    rig = _wav_mode_rig(config, events, "просто болтаю сам с собой")
    await rig.pipeline.start()

    await push(rig.mic, [SILENCE] + [SPEECH] * 8 + [SILENCE] * 6)
    await asyncio.sleep(0.3)

    assert rig.spoken == [], "без слова «Вокс» ничего не выполняется"
    assert rig.pipeline.last_heard == "просто болтаю сам с собой"
    assert IDLE == rig.pipeline._state.state  # noqa: SLF001 - inspect the state machine
    await rig.pipeline.stop()


@pytest.mark.asyncio
async def test_vad_stt_wake_word_alone_starts_listening(config, events):
    rig = _wav_mode_rig(config, events, "Вокс")
    await rig.pipeline.start()

    await push(rig.mic, [SILENCE] + [SPEECH] * 8 + [SILENCE] * 6)
    assert await until(lambda: rig.state.state == LISTENING)
    assert rig.spoken == []
    await rig.pipeline.stop()


@pytest.mark.asyncio
async def test_vad_stt_barge_in_stops_speech_and_runs_the_command(config, events):
    rig = _wav_mode_rig(config, events, "Вокс, который час")
    await rig.pipeline.start()
    rig.pipeline._speaking = True
    rig.state.set(SPEAKING)

    await push(rig.mic, [SILENCE] + [SPEECH] * 8 + [SILENCE] * 6)
    assert await until(lambda: rig.spoken)
    assert rig.tts.stopped >= 1, "озвучка должна быть прервана"
    assert rig.spoken[0] == "который час"
    await rig.pipeline.stop()


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Вокс, открой блокнот", "открой блокнот"),
        ("вокса который час", "который час"),
        ("ВОКС открой доту", "открой доту"),
        ("эй вокс, открой стим", "открой стим"),
        ("vox открой браве", "открой браве"),
        ("привет как дела", None),
        ("окей гугл", None),
        ("", None),
    ],
)
def test_wake_word_stripping(config, heard, expected):
    rig = Rig(config, events=None, wake_mode="vad_stt")
    assert rig.pipeline._strip_wake_word(heard) == expected  # noqa: SLF001 - pure helper


@pytest.mark.asyncio
async def test_vad_stt_ignores_its_own_speech_echo(config, events):
    rig = _wav_mode_rig(config, events, "Привет, я Вокс, так я звучу")
    await rig.pipeline.start()
    rig.pipeline._speaking = True
    rig.pipeline._current_speech = "Привет, я Вокс, так я звучу"
    rig.state.set(SPEAKING)

    await push(rig.mic, [SILENCE] + [SPEECH] * 8 + [SILENCE] * 6)
    await asyncio.sleep(0.3)

    assert rig.spoken == [], "эхо собственной озвучки не должно выполняться"
    await rig.pipeline.stop()


def test_echo_detection_compares_normalised_words(config, events):
    rig = Rig(config, events=None, wake_mode="vad_stt")
    rig.pipeline._current_speech = "Сейчас 14:32, Вокс на связи"
    assert rig.pipeline._is_own_echo("вокс на связи") is True
    assert rig.pipeline._is_own_echo("открой блокнот") is False
    rig.pipeline._current_speech = ""
    assert rig.pipeline._is_own_echo("вокс на связи") is False
