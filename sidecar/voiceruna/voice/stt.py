"""Speech to text with faster-whisper (TZ 5.3).

faster-whisper is imported lazily: the module must import on a machine where
the wheel or the model is missing, and the pipeline then simply reports
"did not catch that" instead of dying.

Rules from the spec:
    model          stt.model (default small)
    device         stt.device (cpu)
    compute_type   stt.compute_type (int8)
    cpu_threads    max(2, cores - 2)
    language       stt.language (ru), beam_size 1
    unload         after stt.unload_after_idle_sec of idleness (TZ 16)

A leading wake word is cut off ("ассистент, открой доту" -> "открой доту"); an empty
result is reported as None so the pipeline can play errors.stt_failed_phrase.
Tests inject a plain callable instead of a model (see the transcriber argument).
"""

from __future__ import annotations

import gc
import importlib.util
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .. import paths
from .audio import resample_int16
from .wake import DEFAULT_PHRASE, normalize_wake_text, strip_wake_word

SAMPLE_RATE = 16000

log = logging.getLogger(__name__)

_PUNCTUATION_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)


def normalize_transcript(text: str) -> str:
    """Collapse whitespace and drop whitespace-only content."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_wake_prefix(text: str, phrase: str = DEFAULT_PHRASE) -> str:
    """Remove a leading "вокс"/"vox" in any case form, with its punctuation."""
    return strip_wake_word(normalize_transcript(text), phrase)


def is_meaningful(text: str) -> bool:
    """False for empty, whitespace-only and punctuation-only transcripts."""
    value = normalize_transcript(text)
    if not value:
        return False
    return _PUNCTUATION_ONLY.sub("", value).strip() != ""


def peak_dbfs(samples: Any) -> float | None:
    """Peak level of int16 PCM in dBFS (-120 for pure silence, None if empty)."""
    data = np.asarray(samples)
    if data.size == 0:
        return None
    peak = float(np.abs(data).max()) / 32768.0
    if peak <= 0.0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(peak))


def _segment_no_speech(segment: Any) -> float | None:
    value = getattr(segment, "no_speech_prob", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover
        return None


def _segment_logprob(segment: Any) -> float | None:
    value = getattr(segment, "avg_logprob", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover
        return None


def _segment_compression(segment: Any) -> float | None:
    value = getattr(segment, "compression_ratio", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover
        return None


def _segment_duration(segment: Any) -> float | None:
    start = getattr(segment, "start", None)
    end = getattr(segment, "end", None)
    try:
        if start is None or end is None:
            return None
        duration = float(end) - float(start)
    except (TypeError, ValueError):  # pragma: no cover
        return None
    return duration if duration > 0 else None


def _text_is_loop(text: str) -> bool:
    """True for decoder loops: 'а а а а', 'лалалала', 'да да да'."""
    words = re.findall(r"[0-9a-zа-яё]+", str(text).lower())
    run = 1
    for previous, current in zip(words, words[1:]):
        run = run + 1 if current == previous else 1
        if run >= 3:
            return True
    compact = "".join(words)
    if len(compact) < 8:
        return False
    for size in (1, 2, 3, 4):
        if len(compact) < size * 4:
            continue
        unit = compact[:size]
        if compact == (unit * (len(compact) // size + 1))[: len(compact)]:
            return True
    return False


def _segment_confidence(segment: Any) -> float | None:
    """Probability-like score for one faster-whisper segment (0..1)."""
    average = getattr(segment, "avg_logprob", None)
    if average is not None:
        try:
            return min(1.0, max(0.0, math.exp(float(average))))
        except (TypeError, ValueError, OverflowError):
            return None
    no_speech = getattr(segment, "no_speech_prob", None)
    if no_speech is not None:
        try:
            return min(1.0, max(0.0, 1.0 - float(no_speech)))
        except (TypeError, ValueError):
            return None
    return None


class WhisperTranscriber:
    """Lazy faster-whisper wrapper with injection hooks for tests."""

    def __init__(
        self,
        config: Any,
        *,
        transcriber: Callable[[np.ndarray], str | None] | None = None,
        model_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._injected = transcriber
        self._model_dir = Path(model_dir) if model_dir else paths.models_dir()
        self._model: Any = None
        self._model_key: tuple[str, str, str] | None = None
        self._last_used = 0.0
        self._language = "ru"
        self._phrase = DEFAULT_PHRASE
        self._last_confidence: float | None = None
        self._last_processing_s = 0.0
        self._last_peak_db: float | None = None
        self._last_normalized = False
        self._normalize_audio = True
        self._normalize_min_peak = 0.0005
        self._normalize_max_gain = 80.0
        self._min_peak_dbfs = -45.0
        self._min_confidence = 0.42
        self._agc_target_peak = 0.95
        # Layered whisper guard (adapted from whisper-guard / Leon).
        self._drop_no_speech = 0.6
        self._drop_logprob = -1.0
        self._extreme_no_speech = 0.85
        self._extreme_logprob = -1.5
        self._max_compression_ratio = 3.0
        self._sparse_min_sec = 10.0
        self._sparse_density = 1.0
        self._rescue_logprob = -0.5
        self._rescue_density = 2.0
        self._rescue_min_sec = 5.0
        self._beam_size = 3
        self._initial_prompt = ""
        self._last_prompt_echo = False
        self._strip_wake = True
        self._unload_after = 300.0
        self.configure(config)

    # -- configuration -----------------------------------------------------
    def configure(self, config: Any | None = None) -> None:
        """Re-read stt.* without dropping an already loaded model."""
        if config is not None:
            self._config = config
        cfg = self._config
        self._language = str(cfg.get("stt.language", "ru") or "ru")
        self._phrase = str(cfg.get("wakeword.phrase", DEFAULT_PHRASE) or DEFAULT_PHRASE)
        try:
            self._unload_after = float(cfg.get("stt.unload_after_idle_sec", 300))
        except (TypeError, ValueError):
            self._unload_after = 300.0
        self._normalize_audio = bool(cfg.get("stt.normalize_audio", True))
        try:
            self._normalize_min_peak = max(0.0, float(cfg.get("stt.normalize_min_peak", 0.0002)))
        except (TypeError, ValueError):  # pragma: no cover
            self._normalize_min_peak = 0.0002
        try:
            self._normalize_max_gain = max(1.0, float(cfg.get("stt.normalize_max_gain", 80.0)))
        except (TypeError, ValueError):  # pragma: no cover
            self._normalize_max_gain = 80.0
        self._min_peak_dbfs = self._float_cfg("stt.min_peak_dbfs", -45.0)
        self._min_confidence = self._float_cfg("stt.min_confidence", 0.42)
        self._agc_target_peak = min(1.0, max(0.1, self._float_cfg("stt.agc_target_peak", 0.95)))
        self._drop_no_speech = self._float_cfg("stt.drop_no_speech", 0.6)
        self._drop_logprob = self._float_cfg("stt.drop_logprob", -1.0)
        self._extreme_no_speech = self._float_cfg("stt.extreme_no_speech", 0.85)
        self._extreme_logprob = self._float_cfg("stt.extreme_logprob", -1.5)
        self._max_compression_ratio = self._float_cfg("stt.max_compression_ratio", 3.0)
        self._sparse_min_sec = self._float_cfg("stt.sparse_min_sec", 10.0)
        self._sparse_density = self._float_cfg("stt.sparse_density", 1.0)
        self._rescue_logprob = self._float_cfg("stt.rescue_logprob", -0.5)
        self._rescue_density = self._float_cfg("stt.rescue_density", 2.0)
        self._rescue_min_sec = self._float_cfg("stt.rescue_min_sec", 5.0)
        try:
            self._beam_size = max(1, int(cfg.get("stt.beam_size", 3) or 1))
        except (TypeError, ValueError):  # pragma: no cover
            self._beam_size = 3
        self._initial_prompt = str(cfg.get("stt.initial_prompt", "") or "")
        self._strip_wake = True
        self._last_prompt_echo = False
        key = self.model_key
        if self._model is not None and self._model_key is not None and key != self._model_key:
            log.info("stt settings changed, dropping the loaded model")
            self.unload()

    def _float_cfg(self, key: str, default: float) -> float:
        try:
            return float(self._config.get(key, default))
        except (TypeError, ValueError):  # pragma: no cover
            return default

    # -- model management --------------------------------------------------
    @property
    def model_name(self) -> str:
        try:
            return str(self._config.get("stt.model", "small") or "small")
        except Exception:  # pragma: no cover
            return "small"

    @property
    def model_key(self) -> tuple[str, str, str]:
        cfg = self._config
        return (
            self.model_name,
            str(cfg.get("stt.device", "cpu") or "cpu"),
            str(cfg.get("stt.compute_type", "int8") or "int8"),
        )

    @property
    def last_confidence(self) -> float | None:
        return self._last_confidence

    @property
    def last_processing_s(self) -> float:
        return self._last_processing_s

    @property
    def last_peak_db(self) -> float | None:
        return self._last_peak_db

    @property
    def last_normalized(self) -> bool:
        return self._last_normalized

    def _normalize(self, samples: np.ndarray) -> tuple[np.ndarray, float | None, bool]:
        """AGC: bring a quiet phrase up to a fixed target peak.

        The peak is normalised to `stt.agc_target_peak` (0.95 by default) with a
        gain cap, so a quiet microphone still gives whisper a normal level.
        Pure digital silence is left untouched; the speech-likeness gate then
        decides whether the buffer is speech at all.
        """
        peak = peak_dbfs(samples)
        if samples.size == 0 or not self._normalize_audio:
            return samples, peak, False
        amplitude = float(np.abs(samples).max()) / 32768.0
        # A far-field USB camera microphone records speech at -50 dBFS; whisper
        # then hallucinates or hears nothing, so quiet speech is amplified up to
        # the configured cap (a pure digital-silence buffer stays untouched).
        if amplitude < self._normalize_min_peak or amplitude >= 0.9:
            return samples, peak, False
        gain = min(self._normalize_max_gain, self._agc_target_peak / amplitude)
        scaled = np.clip(samples.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)
        log.info(
            "stt: agc peak %.1f dBFS -> %.1f dBFS (gain %.2fx)",
            peak if peak is not None else -120.0,
            peak_dbfs(scaled) or -120.0,
            gain,
        )
        return scaled, peak, True

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_path(self) -> Path:
        return self._model_dir / f"whisper-{self.model_name}"

    def _resolved_model(self) -> str:
        local = self.model_path
        if (local / "model.bin").is_file():
            return str(local)
        return self.model_name

    def load(self) -> Any:
        """Create the WhisperModel on first use (blocking, call from a thread)."""
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel  # lazy: heavy, optional

        name, device, compute_type = self.model_key
        threads = max(2, (os.cpu_count() or 4) - 2)
        self._model_dir.mkdir(parents=True, exist_ok=True)
        log.info("loading whisper %s on %s (%s, %d threads)", name, device, compute_type, threads)
        self._model = WhisperModel(
            self._resolved_model(),
            device=device,
            compute_type=compute_type,
            cpu_threads=threads,
            download_root=str(self._model_dir),
        )
        self._model_key = (name, device, compute_type)
        self._last_used = time.monotonic()
        return self._model

    def unload(self) -> None:
        """Free the model (called after stt.unload_after_idle_sec of silence)."""
        if self._model is None:
            return
        log.info("unloading whisper model")
        self._model = None
        self._model_key = None
        gc.collect()

    def maybe_unload(self, now: float | None = None) -> bool:
        """Unload when idle for long enough; returns True when it unloaded."""
        if self._model is None or self._injected is not None:
            return False
        if self._unload_after <= 0:
            return False
        current = time.monotonic() if now is None else float(now)
        if (current - self._last_used) < self._unload_after:
            return False
        self.unload()
        return True

    # -- transcription -----------------------------------------------------
    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str | None:
        """int16 mono PCM -> clean text, or None when nothing was understood."""
        text = self.transcribe_details(audio, sample_rate).get("text") or ""
        return text or None

    def transcribe_details(
        self,
        audio: np.ndarray,
        sample_rate: int = SAMPLE_RATE,
        strip_wake: bool = True,
        min_peak_dbfs: float | None = None,
    ) -> dict:
        """Full recognition report for diagnostics (TZ 5.3).

        Returns {"text", "confidence", "segments", "processing_s", "language"}.

        Confidence formula: for every faster-whisper segment we take
        exp(avg_logprob) (the average log-probability turned back into a 0..1
        probability) and report the mean over the segments; when avg_logprob is
        missing we fall back to 1 - no_speech_prob. The value is clipped to
        0..1 and is None when the engine gives neither number.
        """
        started = time.monotonic()
        # Wake detection needs the wake word in the text; direct commands do not.
        self._strip_wake = bool(strip_wake)
        details: dict = {
            "text": "",
            "confidence": None,
            "segments": 0,
            "processing_s": 0.0,
            "language": self._language,
        }
        samples = np.asarray(audio)
        if samples.size == 0:
            return details
        source_rate = int(sample_rate) or SAMPLE_RATE
        if source_rate != SAMPLE_RATE:
            # STT always gets 16 kHz, whatever the microphone produced.
            samples = resample_int16(samples, source_rate, SAMPLE_RATE)
        samples, peak_db, normalized = self._normalize(samples)
        self._last_peak_db = peak_db
        self._last_normalized = normalized
        details["peak_dbfs"] = peak_db
        details["normalized"] = normalized
        floor = self._min_peak_dbfs if min_peak_dbfs is None else float(min_peak_dbfs)
        if peak_db is not None and peak_db < floor:
            # Below the configured floor it is silence or room noise: whisper
            # would hallucinate a command out of it (owner report 2026-10-28).
            details["processing_s"] = time.monotonic() - started
            details["too_quiet"] = True
            log.info("stt: peak %.1f dBFS is below %.1f, skipping", peak_db, floor)
            self._remember(details, samples.size / float(SAMPLE_RATE), "quiet")
            return details
        details["speech_like"] = speech_like(samples)
        self._last_used = time.monotonic()
        duration_s = samples.size / float(SAMPLE_RATE)
        if not details["speech_like"]:
            # Noise, a fan, keyboard clicks or the echo of our own TTS: running
            # whisper here only produces hallucinations like "ТРЕВОЖНАЯ МУЗЫКА".
            details["processing_s"] = time.monotonic() - started
            log.info("stt: buffer does not look like speech, skipping inference")
            self._remember(details, duration_s, "skip")
            return details
        if self._injected is not None:
            raw = self._injected(samples)
            text = self._finalize(raw) or ""
            details.update(
                {
                    "text": text,
                    "segments": 1 if text else 0,
                    "processing_s": time.monotonic() - started,
                }
            )
            self._remember(details, duration_s, "injected")
            return details

        model = self.load()
        float_audio = samples.astype(np.float32)
        if samples.dtype == np.int16:
            float_audio = float_audio / 32768.0
        segments, info = model.transcribe(
            float_audio,
            language=self._language,
            beam_size=self._beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=self._initial_prompt or None,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        parts: list[str] = []
        confidences: list[float] = []
        dropped = 0
        for segment in segments:
            segment_text = str(getattr(segment, "text", "") or "")
            reason = self._segment_verdict(segment, segment_text)
            if reason is not None:
                log.info("stt: dropping a segment (%s)", reason)
                dropped += 1
                continue
            parts.append(segment_text)
            score = _segment_confidence(segment)
            if score is not None:
                confidences.append(score)
        text = self._finalize(" ".join(parts)) or ""
        mean_confidence = (sum(confidences) / len(confidences)) if confidences else None
        if text and mean_confidence is not None and mean_confidence < self._min_confidence:
            log.info("stt: confidence %.2f is below %.2f, ignoring %r",
                     mean_confidence, self._min_confidence, text[:80])
            text = ""
        if (
            not text
            and not dropped
            and details.get("speech_like")
            and float(details.get("peak_dbfs") or -120.0) > self._min_peak_dbfs
        ):
            # The silence classifier sometimes rejects real quiet speech
            # (no_speech_prob 0.7+, avg_logprob -1.4). A second, relaxed pass
            # recovers it; obvious whisper hallucinations are filtered out.
            relaxed = self._relaxed_pass(model, float_audio)
            if relaxed:
                text = relaxed
                details["relaxed"] = True
        language = str(getattr(info, "language", "") or self._language)
        details.update(
            {
                "text": text,
                "confidence": mean_confidence,
                "segments": len(parts),
                "processing_s": time.monotonic() - started,
                "language": language,
            }
        )
        self._remember(details, duration_s, self.model_key[0])
        return details

    def _segment_verdict(self, segment: Any, text: str, *, relaxed: bool = False) -> str | None:
        """Layered hallucination guard for one decoded segment.

        Principles taken from whisper-guard and Leon (see PROJECT_STATE.md):
        an AND of high no_speech_prob and bad avg_logprob, hard bounds for the
        extremes, compression_ratio for decoder loops, a density check for
        "one phrase stretched over ten seconds", and a rescue for dense,
        confident speech that a noisy bed made look like non-speech.
        """
        if not text or not text.strip():
            return "empty"
        no_speech = _segment_no_speech(segment)
        logprob = _segment_logprob(segment)
        compression = _segment_compression(segment)
        duration = _segment_duration(segment)
        chars = len(re.sub(r"\s+", " ", text).strip())
        density = (chars / duration) if duration and duration > 0.2 else None

        drop_no_speech = 0.8 if relaxed else self._drop_no_speech
        drop_logprob = -1.4 if relaxed else self._drop_logprob
        extreme_no_speech = 0.95 if relaxed else self._extreme_no_speech
        extreme_logprob = -1.8 if relaxed else self._extreme_logprob

        if (
            no_speech is not None
            and no_speech > drop_no_speech
            and logprob is not None
            and logprob > self._rescue_logprob
            and density is not None
            and density >= self._rescue_density
            and duration is not None
            and duration >= self._rescue_min_sec
        ):
            return None
        if no_speech is not None and no_speech > extreme_no_speech:
            return f"no_speech={no_speech:.2f}"
        if logprob is not None and logprob < extreme_logprob:
            return f"logprob={logprob:.2f}"
        if (
            no_speech is not None
            and logprob is not None
            and no_speech > drop_no_speech
            and logprob < drop_logprob
        ):
            return f"no_speech={no_speech:.2f} and logprob={logprob:.2f}"
        if compression is not None and compression > self._max_compression_ratio:
            return f"compression={compression:.1f}"
        if (
            duration is not None
            and density is not None
            and duration >= self._sparse_min_sec
            and density < self._sparse_density
        ):
            return f"sparse {density:.1f} chars/s over {duration:.0f}s"
        return None

    def _relaxed_pass(self, model: Any, float_audio: any) -> str:
        """Second decoding attempt with the silence thresholds disabled."""
        try:
            segments, _info = model.transcribe(
                float_audio,
                language=self._language,
                beam_size=self._beam_size,
                condition_on_previous_text=False,
                initial_prompt=None,
                no_speech_threshold=None,
                log_prob_threshold=None,
            )
            kept: list[str] = []
            for segment in segments:
                segment_text = str(getattr(segment, "text", "") or "")
                # Looser bounds than the main pass (quiet speech often lands at
                # no_speech 0.7 / logprob -1.4) but the structural rules stay.
                if self._segment_verdict(segment, segment_text, relaxed=True) is not None:
                    continue
                kept.append(segment_text)
            raw = " ".join(kept).strip()
        except Exception:  # pragma: no cover - keep the first pass result
            log.debug("stt: relaxed pass failed", exc_info=True)
            return ""
        text = self._finalize(raw) or ""
        if not text:
            return ""
        if _looks_like_hallucination(text):
            log.info("stt: relaxed pass produced a known whisper hallucination %r", text)
            return ""
        return text

    def _remember(self, details: dict, duration_s: float, model_label: str) -> None:
        """Keep the last numbers for diagnostics and log them for the log file."""
        self._last_confidence = details.get("confidence")
        self._last_processing_s = float(details.get("processing_s") or 0.0)
        name, device, compute_type = self.model_key
        log.info(
            "stt: model=%s device=%s compute=%s audio=%.2fs infer=%.2fs segments=%d confidence=%s speech_like=%s text=%r",
            model_label or name,
            device,
            compute_type,
            duration_s,
            self._last_processing_s,
            int(details.get("segments") or 0),
            "n/a" if self._last_confidence is None else f"{self._last_confidence:.3f}",
            details.get("speech_like"),
            str(details.get("text") or "")[:200],
        )

    async def transcribe_async(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str | None:
        """Async wrapper: transcription runs in a worker thread."""
        import asyncio

        return await asyncio.to_thread(self.transcribe, audio, sample_rate)

    def availability(self) -> tuple[bool, str]:
        """Cheap readiness check for diagnostics: never loads the model."""
        if self._injected is not None:
            return True, "транскрайбер подменён"
        try:
            if importlib.util.find_spec("faster_whisper") is None:
                return False, "распознавание речи: пакет faster-whisper не установлен"
        except Exception as exc:  # pragma: no cover - broken environment
            return False, f"распознавание речи: не удалось проверить faster-whisper ({exc})"
        if (self.model_path / "model.bin").is_file():
            return True, f"модель whisper-{self.model_name} на месте"
        return False, f"распознавание речи: модель whisper-{self.model_name} не скачана ({self.model_path})"

    def _finalize(self, raw: str | None) -> str | None:
        text = normalize_transcript(raw)
        if self._is_prompt_echo(text):
            # whisper repeats initial_prompt on noise/silence; treating that as
            # a phrase would make the assistant hear "открой доту" out of nowhere.
            self._last_prompt_echo = True
            return None
        cleaned = strip_wake_word(text, self._phrase) if self._strip_wake else text
        cleaned = normalize_transcript(cleaned)
        if not is_meaningful(cleaned):
            return None
        if _looks_like_hallucination(cleaned):
            # whisper invents these on music, video sound and noise
            self._last_hallucination = cleaned
            return None
        if _text_is_loop(cleaned):
            log.info("stt: decoder loop detected, ignoring %r", cleaned[:80])
            self._last_hallucination = cleaned
            return None
        if not looks_like_a_command(cleaned):
            self._last_hallucination = cleaned
            return None
        return cleaned

    def _is_prompt_echo(self, text: str) -> bool:
        prompt = self._initial_prompt
        if not prompt or not text:
            return False
        words = _word_tokens(text)
        # A real command is short («ассистент, открой блокнот пожалуйста» = 4 words);
        # an echoed prompt is the whole vocabulary list, so only long texts can
        # be echoes.
        if len(words) < 6:
            return False
        vocab = set(_word_tokens(prompt))
        hits = sum(1 for word in words if len(word) > 2 and word in vocab)
        return hits / len(words) >= 0.75

    def close(self) -> None:
        self.unload()


_HALLUCINATIONS = {
    "субтитры", "повторяйте", "продолжение следует", "спасибо за просмотр",
    "подписывайтесь", "ставьте лайки", "редактор субтитров", "корректор",
    "до встречи", "всем пока", "спасибо за внимание", "поехали",
    "повторяйте на видео", "смотрите на видео", "подписывайтесь на канал",
    "не забудьте подписаться", "ставьте лайк", "до встречи в следующем видео",
    "thanks for watching", "subscribe to my channel", "please subscribe",
    "пс4", "пс5", "ps4", "ps5", "playstation", "sony", "xbox", "nintendo",
    "плойка", "плейстейшн", "сони", "плейстейшен",
}

#: Bounds for the second (relaxed) decoding pass.
RELAXED_MAX_NO_SPEECH = 0.85
RELAXED_MIN_LOGPROB = -1.6

#: A one-word transcript is only accepted when it is a real short command.
SINGLE_WORD_COMMANDS = frozenset({"время", "погода", "стоп", "пауза", "хватит", "отмена"})


def looks_like_a_command(text: str) -> bool:
    """False for one-word noise fragments like "дота" or "ps5"."""
    words = _word_tokens(text)
    if not words:
        return False
    if len(words) == 1:
        return words[0] in SINGLE_WORD_COMMANDS
    return True


def speech_like(samples: any, sample_rate: int = SAMPLE_RATE) -> bool:
    """True when the buffer looks like human speech rather than noise.

    Whisper happily "hears" phrases in fan noise, keyboard clicks and the echo
    of our own TTS; those buffers have a flat spectrum and little energy in the
    voice band. Checking before inference keeps phantom commands out of the
    transcript and saves a couple of seconds of CPU.
    """
    try:
        array = np.asarray(samples)
        if array.size < 4000:
            return True
        if np.asarray(array).dtype == np.int16:
            audio = array.astype(np.float32) / 32768.0
        else:
            audio = array.astype(np.float32)
        window_size = min(8192, audio.size)
        # analyse the loudest window: speech may be surrounded by silence
        energy = np.convolve(np.abs(audio), np.ones(window_size) / window_size, mode="valid")
        start = int(np.argmax(energy))
        chunk = audio[start : start + window_size]
        if chunk.size < 1024:
            chunk = audio[:window_size]
        window = np.hanning(chunk.size)
        spectrum = np.abs(np.fft.rfft(chunk * window)) ** 2
        total = float(spectrum.sum())
        if total <= 0.0:
            return False
        freqs = np.fft.rfftfreq(chunk.size, d=1.0 / sample_rate)
        voice = spectrum[(freqs >= 150.0) & (freqs <= 4000.0)]
        voice_ratio = float(voice.sum()) / total
        power = spectrum[1:]
        mean_power = float(np.mean(power)) + 1e-12
        flatness = float(np.exp(np.mean(np.log(power + 1e-12))) / mean_power)
        return voice_ratio >= 0.45 and flatness <= 0.5
    except Exception:  # pragma: no cover - never block recognition on a heuristic
        return True


def _looks_like_hallucination(text: str) -> bool:
    """True for the stock phrases whisper invents on noise and silence."""
    normalized = " ".join(_word_tokens(text))
    if not normalized:
        return True
    if normalized in _HALLUCINATIONS:
        return True
    words = normalized.split()
    if len(words) >= 3 and len(set(words)) == 1:
        return True
    if not any("а" <= char <= "я" for char in normalized):
        return True
    for phrase in _HALLUCINATIONS:
        if len(phrase.split()) >= 2 and normalized.startswith(phrase):
            return True
    tokens = set(normalized.split())
    return bool(tokens & _HALLUCINATIONS)


def _word_tokens(value: str) -> list[str]:
    """Lowercase words without punctuation, ё -> е (prompt-echo detection)."""
    return re.findall(r"[0-9a-zа-я]+", str(value).lower().replace("ё", "е"))


__all__ = [
    "SAMPLE_RATE",
    "WhisperTranscriber",
    "is_meaningful",
    "normalize_transcript",
    "normalize_wake_text",
    "peak_dbfs",
    "strip_wake_prefix",
]