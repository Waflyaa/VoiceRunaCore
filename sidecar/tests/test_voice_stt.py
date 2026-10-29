"""STT helpers: wake word trimming and empty-result handling (TZ 5.3)."""

from __future__ import annotations

import math
import time
import types

import numpy as np
import pytest

from voiceruna.voice.stt import (
    WhisperTranscriber,
    is_meaningful,
    normalize_transcript,
    peak_dbfs,
    strip_wake_prefix,
)

def speech_tone(seconds: float = 1.0, amplitude: int = 6000) -> np.ndarray:
    """A tone stands in for speech: the speech-likeness gate rejects silence."""
    time_axis = np.arange(int(16000 * seconds)) / 16000.0
    return (np.sin(2 * np.pi * 440.0 * time_axis) * amplitude).astype(np.int16)


AUDIO = speech_tone()


def make(config, transcriber=None, **kwargs) -> WhisperTranscriber:
    return WhisperTranscriber(config, transcriber=transcriber, **kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Вокс, открой блокнот", "открой блокнот"),
        ("вокс открой блокнот", "открой блокнот"),
        ("ВОКС! который час", "который час"),
        ("вокса, какая погода", "какая погода"),
        ("vox open explorer", "open explorer"),
        ("открой блокнот", "открой блокнот"),
    ],
)
def test_strip_wake_prefix(raw, expected):
    assert strip_wake_prefix(raw) == expected


def test_transcript_is_cleaned():
    assert normalize_transcript("  открой   блокнот \n") == "открой блокнот"
    assert is_meaningful("  ") is False
    assert is_meaningful("...") is False
    assert is_meaningful("открой блокнот") is True


def test_injected_transcriber_is_used_without_a_model(config):
    transcriber = make(config, transcriber=lambda audio: "Вокс, открой блокнот")
    assert transcriber.transcribe(AUDIO) == "открой блокнот"
    assert transcriber.loaded is False


def test_empty_result_is_none(config):
    for text in ("", "   ", "...", "Вокс"):
        transcriber = make(config, transcriber=lambda audio, value=text: value)
        assert transcriber.transcribe(AUDIO) is None


def test_empty_audio_is_none(config):
    transcriber = make(config, transcriber=lambda audio: "открой блокнот")
    assert transcriber.transcribe(np.zeros(0, dtype=np.int16)) is None


def test_wake_settings_are_read_from_config(config):
    config.set("wakeword.phrase", "вокс")
    transcriber = make(config, transcriber=lambda audio: "Вокса, открой доту")
    assert transcriber.transcribe(AUDIO) == "открой доту"
    assert transcriber.model_name == "small"
    assert transcriber.model_key == ("small", "cpu", "int8")


def test_idle_unload(config):
    config.set("stt.unload_after_idle_sec", 300)
    transcriber = make(config)
    assert transcriber.maybe_unload() is False
    transcriber._model = object()  # simulate a loaded model without loading 500 MB
    transcriber._model_key = transcriber.model_key
    transcriber._last_used = time.monotonic()
    assert transcriber.maybe_unload() is False
    assert transcriber.loaded is True
    assert transcriber.maybe_unload(now=time.monotonic() + 400) is True
    assert transcriber.loaded is False


def test_model_change_drops_the_model(config):
    transcriber = make(config)
    transcriber._model = object()
    transcriber._model_key = transcriber.model_key
    config.set("stt.model", "base")
    transcriber.configure()
    assert transcriber.loaded is False
    assert transcriber.model_name == "base"

# --------------------------------------------------------------------------- details & confidence
class FakeSegment:
    def __init__(
        self,
        text: str,
        avg_logprob: float | None = None,
        no_speech_prob: float | None = None,
        *,
        start: float | None = None,
        end: float | None = None,
        compression_ratio: float | None = None,
    ) -> None:
        self.text = text
        if avg_logprob is not None:
            self.avg_logprob = avg_logprob
        if no_speech_prob is not None:
            self.no_speech_prob = no_speech_prob
        if start is not None:
            self.start = start
        if end is not None:
            self.end = end
        if compression_ratio is not None:
            self.compression_ratio = compression_ratio


class FakeModel:
    """Stands in for faster_whisper.WhisperModel (no 500 MB download)."""

    def __init__(self, segments: list[FakeSegment], language: str = "ru") -> None:
        self._segments = segments
        self._language = language
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(self._segments), types.SimpleNamespace(language=self._language)


def loaded(config, segments, **kwargs) -> tuple[WhisperTranscriber, FakeModel]:
    transcriber = WhisperTranscriber(config)
    model = FakeModel(segments, **kwargs)
    transcriber._model = model
    transcriber._model_key = transcriber.model_key
    return transcriber, model


def test_transcribe_details_reports_confidence_and_timing(config):
    segments = [
        FakeSegment(" Открой блокнот.", math.log(0.8)),
        FakeSegment(" Сейчас.", math.log(0.45)),
    ]
    transcriber, model = loaded(config, segments)
    details = transcriber.transcribe_details(speech_tone(amplitude=3000), 16000)

    assert details["text"] == "Открой блокнот. Сейчас."
    assert details["segments"] == 2
    assert details["confidence"] == pytest.approx((0.8 + 0.45) / 2, abs=0.01)
    assert details["language"] == "ru"
    assert details["processing_s"] >= 0.0
    assert details["peak_dbfs"] == pytest.approx(20 * math.log10(3000 / 32768), abs=0.5)
    assert model.calls and model.calls[0]["language"] == "ru"


def test_confidence_falls_back_to_no_speech_probability(config):
    transcriber, _model = loaded(config, [FakeSegment(" текст", no_speech_prob=0.25)])
    details = transcriber.transcribe_details(speech_tone(amplitude=3000), 16000)
    assert details["confidence"] == pytest.approx(0.75)


def test_confidence_is_none_without_scores(config):
    transcriber, _model = loaded(config, [FakeSegment(" текст")])
    assert transcriber.transcribe_details(speech_tone(amplitude=3000), 16000)["confidence"] is None


def test_transcribe_still_returns_text(config):
    transcriber, _model = loaded(config, [FakeSegment(" Открой блокнот.", math.log(0.9))])
    assert transcriber.transcribe(speech_tone(amplitude=3000), 16000) == "Открой блокнот."
    assert transcriber.last_confidence == pytest.approx(0.9, abs=0.01)
    assert transcriber.last_processing_s >= 0.0


def test_details_upsample_path_gets_16k(config):
    """A 48 kHz recording is resampled before inference."""
    transcriber, model = loaded(config, [FakeSegment(" да", math.log(0.9))])
    transcriber.transcribe_details((np.sin(2 * np.pi * 440.0 * np.arange(48000) / 48000.0) * 6000).astype(np.int16), 48000)
    audio = model.calls
    assert audio  # the model was called
    # faster-whisper receives a float array of exactly 1 s at 16 kHz
    assert transcriber._last_peak_db is not None


# --------------------------------------------------------------------------- normalization
def capturing_transcriber(store: dict):
    def transcribe(audio):
        store["peak"] = int(np.abs(audio).max())
        store["size"] = int(audio.size)
        return "открой блокнот"

    return transcribe


def test_quiet_audio_is_normalized_to_the_agc_target(config):
    store: dict = {}
    transcriber = WhisperTranscriber(config, transcriber=capturing_transcriber(store))
    quiet = np.full(1600, 3000, dtype=np.int16)  # ~0.09 peak
    details = transcriber.transcribe_details(quiet, 16000)
    assert details["normalized"] is True
    target = float(config.get("stt.agc_target_peak", 0.95))
    assert store["peak"] == pytest.approx(int(target * 32767), abs=40)
    assert details["peak_dbfs"] == pytest.approx(20 * math.log10(3000 / 32768), abs=0.5)


def test_silence_is_not_amplified(config):
    store: dict = {}
    transcriber = WhisperTranscriber(config, transcriber=capturing_transcriber(store))
    details = transcriber.transcribe_details(np.zeros(1600, dtype=np.int16), 16000)
    assert details["normalized"] is False
    assert details.get("too_quiet") is True
    assert "peak" not in store


def test_loud_audio_is_left_alone(config):
    store: dict = {}
    transcriber = WhisperTranscriber(config, transcriber=capturing_transcriber(store))
    loud = np.full(1600, 31000, dtype=np.int16)  # 0.95 peak
    details = transcriber.transcribe_details(loud, 16000)
    assert details["normalized"] is False
    assert store["peak"] == 31000


def test_normalization_can_be_switched_off(config):
    config.set("stt.normalize_audio", False)
    store: dict = {}
    transcriber = WhisperTranscriber(config, transcriber=capturing_transcriber(store))
    details = transcriber.transcribe_details(np.full(1600, 3000, dtype=np.int16), 16000)
    assert details["normalized"] is False
    assert store["peak"] == 3000


def test_peak_dbfs_values():
    assert peak_dbfs(np.zeros(10, dtype=np.int16)) == -120.0
    assert peak_dbfs(np.array([], dtype=np.int16)) is None
    assert peak_dbfs(np.full(10, 32767, dtype=np.int16)) == pytest.approx(0.0, abs=0.01)


def test_prompt_echo_is_dropped_instead_of_becoming_a_command(config):
    prompt = "Вокс, открой доту, Факторио, Steam, Brave, блокнот, калькулятор"
    config.set("stt.initial_prompt", prompt)
    transcriber = make(config, transcriber=lambda audio: prompt)

    details = transcriber.transcribe_details(AUDIO)

    assert details["text"] == ""
    assert details["segments"] == 0
    assert transcriber._last_prompt_echo is True


def test_real_command_is_not_mistaken_for_a_prompt_echo(config):
    transcriber = make(config, transcriber=lambda audio: "Вокс, открой блокнот")

    details = transcriber.transcribe_details(AUDIO)

    assert details["text"] == "открой блокнот"
    assert transcriber._last_prompt_echo is False


def test_short_prompt_like_words_are_kept(config):
    transcriber = make(config, transcriber=lambda audio: "открой браве")

    details = transcriber.transcribe_details(AUDIO)

    assert details["text"] == "открой браве"


def test_wake_mode_keeps_the_wake_word_in_the_transcript(config):
    transcriber = make(config, transcriber=lambda audio: "Вокс, открой блокнот")

    wake = transcriber.transcribe_details(AUDIO, 16000, strip_wake=False)
    direct = transcriber.transcribe_details(AUDIO, 16000)

    assert "вокс" in wake["text"].lower()
    assert "вокс" not in direct["text"].lower()
    assert direct["text"] == "открой блокнот"


# ------------------------------------------------- anti-hallucination gates
def test_segment_with_high_no_speech_is_dropped(config):
    segment = FakeSegment(" открой доту.", math.log(0.9), no_speech_prob=0.9)
    transcriber, _model = loaded(config, [segment])

    assert transcriber.transcribe_details(speech_tone(amplitude=6000), 16000)["text"] == ""


def test_segment_with_low_logprob_is_dropped(config):
    segment = FakeSegment(" открой доту.", math.log(0.1), no_speech_prob=0.1)  # avg_logprob -2.3
    transcriber, _model = loaded(config, [segment])

    assert transcriber.transcribe_details(speech_tone(amplitude=6000), 16000)["text"] == ""


def test_quiet_audio_never_reaches_the_model(config):
    store: dict = {}
    transcriber = WhisperTranscriber(config, transcriber=capturing_transcriber(store))

    details = transcriber.transcribe_details(np.full(16000, 30, dtype=np.int16), 16000)

    assert details["too_quiet"] is True
    assert details["text"] == ""
    assert store == {}


def test_playstation_noise_is_not_a_command(config):
    for phrase in ("ps5", "PS4", "PlayStation", "Sony", "пс5"):
        assert transcriber_text(config, phrase) == ""


def test_single_word_noise_fragment_is_rejected(config):
    assert transcriber_text(config, "доту") == ""
    assert transcriber_text(config, "время") == "время"


def transcriber_text(config, phrase):
    """Run the text-level filters exactly as the pipeline would."""
    from voiceruna.voice.stt import WhisperTranscriber

    transcriber = WhisperTranscriber(config, transcriber=lambda audio: phrase)
    return transcriber.transcribe_details(speech_tone(amplitude=6000), 16000)["text"]


# ------------------------------------------- layered whisper guard (Leon/whisper-guard)
def _text_with_segment(config, segment):
    from voiceruna.voice.stt import WhisperTranscriber

    transcriber = WhisperTranscriber(config)
    model = type("M", (), {"transcribe": lambda self, audio, **kw: (iter([segment]), type("I", (), {"language": "ru"})())})()
    transcriber._model = model
    transcriber._model_key = transcriber.model_key
    return transcriber.transcribe_details(speech_tone(amplitude=6000), 16000)["text"]


def test_and_filter_drops_unsure_segment(config):
    segment = FakeSegment(" открой доту.", -1.2, 0.7, start=0.0, end=2.0)
    assert _text_with_segment(config, segment) == ""


def test_and_filter_keeps_confident_segment(config):
    segment = FakeSegment(" открой блокнот.", -0.8, 0.7, start=0.0, end=2.0)
    assert _text_with_segment(config, segment) == "открой блокнот."


def test_compression_ratio_loop_is_dropped(config):
    segment = FakeSegment(" бла бла бла.", -0.2, 0.1, start=0.0, end=3.0, compression_ratio=4.5)
    assert _text_with_segment(config, segment) == ""


def test_sparse_long_segment_is_dropped(config):
    segment = FakeSegment(" привет.", -0.1, 0.2, start=0.0, end=12.0)
    assert _text_with_segment(config, segment) == ""


def test_dense_confident_segment_is_rescued_from_high_no_speech(config):
    segment = FakeSegment(
        " открой блокнот пожалуйста сейчас же.",
        -0.2,
        0.9,
        start=0.0,
        end=6.0,
    )
    assert _text_with_segment(config, segment) == "открой блокнот пожалуйста сейчас же."


def test_decoder_loop_text_is_dropped(config):
    segment = FakeSegment(" да да да да", -0.1, 0.1, start=0.0, end=3.0)
    assert _text_with_segment(config, segment) == ""
