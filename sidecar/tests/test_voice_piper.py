"""PiperTts: availability, voice download and synthesis (TZ 5.4).

No test touches the network or the real 63 MB voice: HTTP goes through
httpx.MockTransport, and synthesis uses a fake voice object.  The real engine
is exercised separately on the developer machine (see the task report).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from voiceruna.voice import piper_tts
from voiceruna.voice.piper_tts import (
    CONFIG_FILENAME,
    MODEL_FILENAME,
    SAMPLE_RATE,
    PiperError,
    PiperTts,
)

VOICE_CONFIG = json.dumps(
    {
        "audio": {"sample_rate": SAMPLE_RATE},
        "espeak": {"voice": "ru"},
        "num_symbols": 4,
        "num_speakers": 1,
        "phoneme_id_map": {"_": [0], "^": [1], "$": [2], "a": [3]},
    },
    ensure_ascii=False,
).encode("utf-8")
#: First bytes of a protobuf ONNX file (ir_version varint) and enough padding.
MODEL_PAYLOAD = b"\x08\x07" + b"\x00" * 64


@pytest.fixture()
def small_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator expects a >=50 MB model; tests use a tiny stand-in."""
    monkeypatch.setattr(piper_tts, "MODEL_MIN_BYTES", 4)
    monkeypatch.setattr(piper_tts, "MODEL_MAX_BYTES", 1_000_000)


def voice_handler(calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/" + MODEL_FILENAME):
            return httpx.Response(200, content=MODEL_PAYLOAD)
        if path.endswith("/" + CONFIG_FILENAME):
            return httpx.Response(200, content=VOICE_CONFIG)
        return httpx.Response(404)

    return handler


def make_tts(config, handler=None, **kwargs) -> PiperTts:
    kwargs.setdefault("retry_delays", (0.0,))
    if handler is not None:
        transport = httpx.MockTransport(handler)
        kwargs.setdefault(
            "client_factory",
            lambda: httpx.AsyncClient(transport=transport),
        )
    return PiperTts(config, **kwargs)


# ------------------------------------------------------------------ availability
def test_availability_reports_missing_package(config, data_dir, monkeypatch):
    monkeypatch.setattr(piper_tts, "_piper_installed", lambda: False)
    tts = PiperTts(config)
    ready, reason = tts.availability()
    assert ready is False
    assert "piper-tts" in reason
    assert "не установлен" in reason


def test_availability_reports_missing_voice(config, data_dir, monkeypatch):
    monkeypatch.setattr(piper_tts, "_piper_installed", lambda: True)
    tts = PiperTts(config)
    ready, reason = tts.availability()
    assert ready is False
    assert "не скачан" in reason
    assert MODEL_FILENAME in reason


def test_availability_is_true_for_a_downloaded_voice(config, data_dir, small_model, monkeypatch):
    monkeypatch.setattr(piper_tts, "_piper_installed", lambda: True)
    tts = PiperTts(config)
    tts.voice_dir.mkdir(parents=True)
    Path(tts.voice_path()).write_bytes(MODEL_PAYLOAD)
    Path(tts.config_path()).write_bytes(VOICE_CONFIG)
    ready, reason = tts.availability()
    assert ready is True
    assert "готов" in reason


def test_espeak_ng_data_is_bundled_with_the_wheel():
    """The chosen route phonemizes without a system espeak-ng install."""
    piper = pytest.importorskip("piper")
    directory = piper_tts._espeak_data_dir(piper)
    assert (directory / "ru_dict").is_file()
    assert (directory / "phondata").is_file()


def test_availability_rejects_a_broken_config(config, data_dir, small_model, monkeypatch):
    monkeypatch.setattr(piper_tts, "_piper_installed", lambda: True)
    tts = PiperTts(config)
    tts.voice_dir.mkdir(parents=True)
    Path(tts.voice_path()).write_bytes(MODEL_PAYLOAD)
    Path(tts.config_path()).write_bytes(b"<html>not a voice</html>")
    ready, reason = tts.availability()
    assert ready is False
    assert "повреждён" in reason


# -------------------------------------------------------------------- download
async def test_ensure_voice_downloads_both_files_once(
    config, events, data_dir, small_model
):
    calls: list[str] = []
    tts = make_tts(config, voice_handler(calls))
    assert await tts.ensure_voice(events) is True
    model = Path(tts.voice_path())
    config_file = Path(tts.config_path())
    assert model.read_bytes() == MODEL_PAYLOAD
    assert config_file.read_bytes() == VOICE_CONFIG
    assert model.parent == tts.voice_dir
    assert model.parent.name == "piper"
    assert len(calls) == 2

    assert await tts.ensure_voice(events) is True
    assert len(calls) == 2  # cached: no second HTTP request
    assert tts.availability()[0] is True

    progress = [
        event for event in events.collected if event.type == "download_progress"
    ]
    assert progress
    assert all(event.data["stage"] == "piper" for event in progress)
    assert progress[-1].data["percent"] == pytest.approx(100.0)
    percents = [float(event.data["percent"]) for event in progress]
    assert percents == sorted(percents)


async def test_network_failure_reports_error_and_returns_false(
    config, events, data_dir, small_model
):
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    tts = make_tts(config, broken)
    assert await tts.ensure_voice(events) is False
    errors = [event for event in events.collected if event.type == "error"]
    assert errors
    assert errors[0].data["kind"] == "download"
    assert "HTTPStatusError" in errors[0].data["message"]
    assert not Path(tts.voice_path()).exists()
    assert not list(tts.voice_dir.glob("*.part"))


async def test_html_instead_of_onnx_is_rejected(config, events, data_dir, small_model):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/" + MODEL_FILENAME):
            return httpx.Response(200, content=b"<html>rate limited</html>")
        return httpx.Response(200, content=VOICE_CONFIG)

    tts = make_tts(config, handler)
    assert await tts.ensure_voice(events) is False
    errors = [event for event in events.collected if event.type == "error"]
    assert errors and errors[0].data["kind"] == "download"
    assert Path(tts.voice_path()).exists()  # the bytes are cached for inspection
    assert tts.availability()[0] is False


# ------------------------------------------------------------------- synthesis
class FakeChunk:
    def __init__(self, samples, rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = rate
        self.audio_int16_array = np.asarray(samples, dtype=np.int16)


class FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: BaseException | None = None
        self.chunks = [FakeChunk([100, -100, 200])]

    def synthesize(self, text: str, syn_config: object = None):
        self.calls.append((text, syn_config))
        if self.error is not None:
            raise self.error
        yield from self.chunks


def test_synthesize_returns_int16_pcm_at_22050(config, data_dir):
    voice = FakeVoice()
    tts = PiperTts(config, voice_factory=lambda: voice)
    pcm, rate = tts.synthesize("Проверка локального голоса")
    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.int16
    assert pcm.ndim == 1
    assert pcm.tolist() == [100, -100, 200]
    assert rate == 22_050
    assert voice.calls[0][0] == "Проверка локального голоса"


def test_synthesize_concatenates_sentences(config, data_dir):
    voice = FakeVoice()
    voice.chunks = [FakeChunk([1, 2]), FakeChunk([3])]
    tts = PiperTts(config, voice_factory=lambda: voice)
    pcm, rate = tts.synthesize("Раз. Два.")
    assert pcm.tolist() == [1, 2, 3]
    assert rate == SAMPLE_RATE


def test_empty_text_returns_empty_array_without_loading_the_voice(config, data_dir):
    loaded: list[int] = []

    def factory() -> FakeVoice:
        loaded.append(1)
        return FakeVoice()

    tts = PiperTts(config, voice_factory=factory)
    pcm, rate = tts.synthesize("   \n\t ")
    assert pcm.size == 0
    assert pcm.dtype == np.int16
    assert rate == SAMPLE_RATE
    assert loaded == []


def test_text_longer_than_the_limit_is_rejected(config, data_dir):
    tts = PiperTts(config, voice_factory=lambda: FakeVoice())
    with pytest.raises(ValueError, match="500"):
        tts.synthesize("а" * 501)


def test_synthesizer_error_is_raised_with_russian_text(config, data_dir):
    voice = FakeVoice()
    voice.error = RuntimeError("boom")
    tts = PiperTts(config, voice_factory=lambda: voice)
    with pytest.raises(PiperError) as info:
        tts.synthesize("Проверка")
    assert "не удалось синтезировать" in str(info.value)
    assert "boom" in str(info.value)
    assert isinstance(info.value.__cause__, RuntimeError)


def test_warm_up_is_idempotent_and_reloads_after_close(config, data_dir):
    created: list[FakeVoice] = []

    def factory() -> FakeVoice:
        voice = FakeVoice()
        created.append(voice)
        return voice

    tts = PiperTts(config, voice_factory=factory)
    tts.warm_up()
    tts.warm_up()
    assert len(created) == 1
    tts.close()
    tts.warm_up()
    assert len(created) == 2


def test_warm_up_never_raises(config, data_dir):
    def factory() -> FakeVoice:
        raise piper_tts.PiperUnavailable("голос не скачан")

    tts = PiperTts(config, voice_factory=factory)
    tts.warm_up()  # a background warm-up must not kill its thread


def test_rate_setting_maps_to_piper_length_scale(config, data_dir):
    tts = PiperTts(config)
    assert tts._length_scale() == pytest.approx(100.0 / 110.0)  # tts.rate = +10%
    config.set("tts.rate", "-20%")
    assert tts._length_scale() == pytest.approx(1.25)
    config.set("tts.rate", "без процентов")
    assert tts._length_scale() == pytest.approx(1.0)
