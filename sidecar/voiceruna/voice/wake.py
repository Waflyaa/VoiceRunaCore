"""Wake word detection (TZ 2.6, 5.1).

Engines, all offline and all optional:
    vosk        Vosk KWS with a grammar built from wakeword.phrase (default)
    porcupine   Picovoice Porcupine, needs pvporcupine + PORCUPINE_ACCESS_KEY
    openwakeword  openWakeWord with a custom .onnx model
    none        no wake word at all, the hotkey is the only way in

Every engine hides behind the tiny WakeBackend protocol (feed(frame) -> score
or None), so WakeDetector - which owns the threshold and the barge-in bump -
is testable with a one-line fake backend and no model files.

Threshold rule (TZ 2.6): a hit needs score >= wakeword.threshold normally and
score >= wakeword.threshold + wakeword.bargein_bump while TTS is playing.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .. import paths
from .audio import SAMPLE_RATE
from .download import VOSK_MODEL_DIR

DEFAULT_PHRASE = "вокс"
DEFAULT_THRESHOLD = 0.5
DEFAULT_BARGEIN_BUMP = 0.15

#: Extra stems accepted for the configured phrase (Russian / English pair).
_ALTERNATE_STEMS: dict[str, tuple[str, ...]] = {
    "вокс": ("vox",),
    "vox": ("вокс",),
}

#: Nominative and the cases a Russian speaker actually uses for the name.
_CASE_SUFFIX = r"(?:а|у|ом|ем|е|ой|ы|и|ам|ами|ах|ов)?"
_CASE_FORMS = ("", "а", "у", "ом", "е", "и", "ы")
_LEFT_EDGE = r"(?<![0-9a-zа-яё])"
_RIGHT_EDGE = r"(?![0-9a-zа-яё])"
_LEADING_JUNK = r"^[\s,\.\!\?\-–—:;…\"'«»()]+"

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- text
def _is_cyrillic(text: str) -> bool:
    """True when the string contains at least one Cyrillic letter."""
    return any("а" <= ch <= "я" or ch == "ё" for ch in str(text).lower())


def normalize_wake_text(text: str) -> str:
    """Lower-case, ё -> е, punctuation to spaces, whitespace collapsed."""
    value = str(text or "").lower().replace("ё", "е")
    value = re.sub(r"[^\w\s\-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def wake_stems(phrase: str = DEFAULT_PHRASE) -> tuple[str, ...]:
    """Stems that count as the wake word, including the Latin spelling."""
    normalized = normalize_wake_text(phrase) or DEFAULT_PHRASE
    stems: list[str] = []
    for word in normalized.split():
        stems.append(word)
        stems.extend(_ALTERNATE_STEMS.get(word, ()))
    return tuple(dict.fromkeys(stems))


def wake_pattern(phrase: str = DEFAULT_PHRASE) -> re.Pattern[str]:
    """Compiled regex matching the wake word in any case form."""
    stems = wake_stems(phrase)
    core = "|".join(re.escape(stem) + _CASE_SUFFIX for stem in stems)
    return re.compile(_LEFT_EDGE + "(?:" + core + ")" + _RIGHT_EDGE, re.UNICODE | re.IGNORECASE)


def matches_wake_word(text: str, phrase: str = DEFAULT_PHRASE) -> bool:
    """True when the text contains the wake word in any case form."""
    return bool(wake_pattern(phrase).search(normalize_wake_text(text)))


def strip_wake_word(text: str, phrase: str = DEFAULT_PHRASE) -> str:
    """Drop a leading wake word together with its punctuation.

    "ассистент, открой доту" -> "открой доту"
    "вокса который час" -> "который час"
    "VOX, open"          -> "open"
    """
    value = str(text or "").strip()
    if not value:
        return ""
    match = wake_pattern(phrase).match(value)
    if match is None:
        return value
    rest = value[match.end() :]
    return re.sub(_LEADING_JUNK, "", rest).strip()


# --------------------------------------------------------------------------- backends
@runtime_checkable
class WakeBackend(Protocol):
    """One wake word engine.

    feed() returns a confidence in 0..1 (True counts as 1.0) or None when the
    engine produced no hit for this frame.
    """

    name: str

    def feed(self, frame: np.ndarray) -> float | None:
        ...

    def reset(self) -> None:
        ...

    def close(self) -> None:
        ...


class NoneBackend:
    """No wake word: the hotkey / POST /listen is the only activation."""

    name = "none"

    def feed(self, frame: np.ndarray) -> float | None:
        return None

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeBackend:
    """Fixed-score backend used by tests to exercise the threshold logic."""

    name = "fake"

    def __init__(self, score: float | None = 0.0) -> None:
        self.score = score
        self.frames = 0

    def feed(self, frame: np.ndarray) -> float | None:
        self.frames += 1
        return self.score

    def reset(self) -> None:
        self.frames = 0

    def close(self) -> None:
        return None


class VoskBackend:
    """Vosk KWS: a grammar restricted to the wake word keeps CPU and misses low."""

    name = "vosk"

    def __init__(
        self,
        model_path: str | Path,
        *,
        phrase: str = DEFAULT_PHRASE,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._model_path = Path(model_path)
        self._phrase = phrase
        self._sample_rate = int(sample_rate)
        self._model: Any = None
        self._recognizer: Any = None
        self._failed = False
        self.last_heard = ""

    def _ensure(self) -> bool:
        if self._recognizer is not None:
            return True
        if self._failed:
            return False
        try:
            import vosk
        except Exception as exc:  # pragma: no cover - optional dependency
            log.info("vosk is not installed (%s)", exc)
            self._failed = True
            return False
        try:
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(str(self._model_path))
            grammar = json.dumps(self._grammar(), ensure_ascii=False)
            self._recognizer = vosk.KaldiRecognizer(self._model, self._sample_rate, grammar)
        except Exception as exc:  # pragma: no cover
            log.warning("vosk KWS unavailable: %s", exc)
            self._failed = True
            return False
        return True

    def _grammar(self) -> list[str]:
        """KWS grammar: Russian case forms plus the bare Latin spelling.

        Case suffixes only make sense on the Cyrillic stem - "voxу" is not a
        Russian word and Vosk would drop it with a warning on every start.
        """
        words: list[str] = []
        for stem in wake_stems(self._phrase):
            forms = _CASE_FORMS if _is_cyrillic(stem) else ("",)
            for suffix in forms:
                word = stem + suffix
                if word not in words:
                    words.append(word)
        return words + ["[unk]"]

    def feed(self, frame: np.ndarray) -> float | None:
        if not self._ensure():
            return None
        try:
            data = np.asarray(frame, dtype=np.int16).tobytes()
            if self._recognizer.AcceptWaveform(data):
                text = str(json.loads(self._recognizer.Result()).get("text", ""))
                if text:
                    self.last_heard = text
                    log.debug("vosk final: %r", text)
                if matches_wake_word(text, self._phrase):
                    return 1.0
            partial = str(json.loads(self._recognizer.PartialResult()).get("partial", ""))
            if partial:
                self.last_heard = partial
                log.debug("vosk partial: %r", partial)
            if matches_wake_word(partial, self._phrase):
                return 1.0
        except Exception:  # pragma: no cover - a broken recognizer must not kill audio
            log.debug("vosk feed failed", exc_info=True)
        return None

    def reset(self) -> None:
        if self._recognizer is not None:
            try:
                self._recognizer.Reset()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self._recognizer = None
        self._model = None


class PorcupineBackend:
    """Picovoice Porcupine; requires pvporcupine and a PORCUPINE_ACCESS_KEY."""

    name = "porcupine"

    def __init__(
        self,
        access_key: str,
        *,
        keyword_paths: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        self._access_key = access_key
        self._keyword_paths = keyword_paths or []
        self._keywords = keywords or []
        self._handle: Any = None
        self._failed = False

    def _ensure(self) -> bool:
        if self._handle is not None:
            return True
        if self._failed:
            return False
        try:
            import pvporcupine
        except Exception as exc:  # pragma: no cover - optional dependency
            log.info("pvporcupine is not installed (%s)", exc)
            self._failed = True
            return False
        try:
            kwargs: dict[str, Any] = {"access_key": self._access_key}
            if self._keyword_paths:
                kwargs["keyword_paths"] = self._keyword_paths
            else:
                kwargs["keywords"] = self._keywords or ["jarvis"]
            self._handle = pvporcupine.create(**kwargs)
        except Exception as exc:  # pragma: no cover
            log.warning("porcupine unavailable: %s", exc)
            self._failed = True
            return False
        return True

    def feed(self, frame: np.ndarray) -> float | None:
        if not self._ensure():
            return None
        try:
            samples = np.asarray(frame, dtype=np.int16)
            size = int(getattr(self._handle, "frame_length", samples.size))
            if samples.size != size:
                samples = np.resize(samples, size)
            index = int(self._handle.process(samples.tolist()))
        except Exception:  # pragma: no cover
            return None
        return 1.0 if index >= 0 else None

    def reset(self) -> None:
        return None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.delete()
            except Exception:  # pragma: no cover
                pass
        self._handle = None


class OpenWakeWordBackend:
    """openWakeWord with a bundled .onnx model for the wake word."""

    name = "openwakeword"

    def __init__(self, model_path: str | Path) -> None:
        self._model_path = Path(model_path)
        self._model: Any = None
        self._failed = False

    def _ensure(self) -> bool:
        if self._model is not None:
            return True
        if self._failed:
            return False
        try:
            from openwakeword.model import Model
        except Exception as exc:  # pragma: no cover - optional dependency
            log.info("openwakeword is not installed (%s)", exc)
            self._failed = True
            return False
        try:
            self._model = Model(wakeword_models=[str(self._model_path)])
        except Exception as exc:  # pragma: no cover
            log.warning("openwakeword unavailable: %s", exc)
            self._failed = True
            return False
        return True

    def feed(self, frame: np.ndarray) -> float | None:
        if not self._ensure():
            return None
        try:
            scores = self._model.predict(np.asarray(frame, dtype=np.int16))
        except Exception:  # pragma: no cover
            return None
        if not scores:
            return None
        return max(float(value) for value in scores.values())

    def reset(self) -> None:
        self._model = None

    def close(self) -> None:
        self._model = None


# --------------------------------------------------------------------------- detector
class WakeDetector:
    """Engine + threshold policy (normal threshold, raised during TTS)."""

    def __init__(
        self,
        config: Any,
        *,
        backend: WakeBackend | None = None,
        cooldown_sec: float = 0.3,
    ) -> None:
        self._config = config
        self._backend: WakeBackend = backend if backend is not None else NoneBackend()
        self._cooldown = max(0.0, float(cooldown_sec))
        self._last_hit = -1e9
        self._threshold = DEFAULT_THRESHOLD
        self._bump = DEFAULT_BARGEIN_BUMP
        self._phrase = DEFAULT_PHRASE
        self.configure(config)

    # -- configuration -----------------------------------------------------
    def configure(self, config: Any | None = None) -> None:
        cfg = config if config is not None else self._config
        self._config = cfg
        self._threshold = float(cfg.get("wakeword.threshold", DEFAULT_THRESHOLD))
        self._bump = float(cfg.get("wakeword.bargein_bump", DEFAULT_BARGEIN_BUMP))
        self._phrase = str(cfg.get("wakeword.phrase", DEFAULT_PHRASE) or DEFAULT_PHRASE)

    @property
    def backend(self) -> WakeBackend:
        return self._backend

    @property
    def engine(self) -> str:
        return str(getattr(self._backend, "name", "none"))

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def bargein_bump(self) -> float:
        return self._bump

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def last_heard(self) -> str:
        """What the engine last recognised, raw (diagnostics)."""
        return str(getattr(self._backend, "last_heard", "") or "")

    def threshold_for(self, barge_in: bool = False) -> float:
        """Effective threshold; TTS playback raises the bar by bargein_bump."""
        return self._threshold + (self._bump if barge_in else 0.0)

    # -- detection ---------------------------------------------------------
    def feed(self, frame: np.ndarray, *, barge_in: bool = False) -> bool:
        backend = self._backend
        if backend is None:
            return False
        try:
            score = backend.feed(frame)
        except Exception:  # pragma: no cover - never break the audio loop
            log.debug("wake backend failed", exc_info=True)
            return False
        if score is None:
            return False
        try:
            value = float(score)
        except (TypeError, ValueError):
            return False
        if value < self.threshold_for(barge_in):
            return False
        now = time.monotonic()
        if self._cooldown > 0 and (now - self._last_hit) < self._cooldown:
            return False
        self._last_hit = now
        reset = getattr(backend, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:  # pragma: no cover
                pass
        return True

    def reset(self) -> None:
        self._last_hit = -1e9
        reset = getattr(self._backend, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------------------- factory
def vosk_available() -> bool:
    """True when the vosk wheel is importable (model may still be missing)."""
    return importlib.util.find_spec("vosk") is not None


def vosk_model_path(model_dir: str | Path | None = None) -> Path:
    root = Path(model_dir) if model_dir else paths.models_dir()
    return root / VOSK_MODEL_DIR


def create_wake_detector(config: Any, *, model_dir: str | Path | None = None) -> WakeDetector:
    """Build the configured engine, degrading to hotkey-only instead of failing.

    The spike (docs/spike-wakeword.md) picked vosk as the default engine with
    the hotkey as the only fallback, so an unusable vosk install must leave the
    assistant fully usable rather than broken.
    """
    engine = str(config.get("wakeword.engine", "vosk") or "none").strip().lower()
    phrase = str(config.get("wakeword.phrase", DEFAULT_PHRASE) or DEFAULT_PHRASE)
    backend: WakeBackend = NoneBackend()

    if engine == "vosk":
        path = vosk_model_path(model_dir)
        if vosk_available() and path.is_dir():
            backend = VoskBackend(path, phrase=phrase)
        else:
            log.info("vosk wake word unavailable (model or package missing), hotkey only")
    elif engine == "porcupine":
        key = ""
        try:
            key = str(config.env_value("PORCUPINE_ACCESS_KEY") or "")
        except Exception:  # pragma: no cover - config stub without .env helpers
            key = ""
        if key and importlib.util.find_spec("pvporcupine") is not None:
            backend = PorcupineBackend(key)
        else:
            log.info("porcupine wake word unavailable, hotkey only")
    elif engine == "openwakeword":
        root = Path(model_dir) if model_dir else paths.models_dir()
        model = root / "openwakeword" / "vox.onnx"
        if model.is_file() and importlib.util.find_spec("openwakeword") is not None:
            backend = OpenWakeWordBackend(model)
        else:
            log.info("openwakeword model unavailable, hotkey only")
    elif engine != "none":
        log.info("unknown wake engine %r, hotkey only", engine)

    return WakeDetector(config, backend=backend)


__all__ = [
    "DEFAULT_BARGEIN_BUMP",
    "DEFAULT_PHRASE",
    "DEFAULT_THRESHOLD",
    "FakeBackend",
    "NoneBackend",
    "OpenWakeWordBackend",
    "PorcupineBackend",
    "VoskBackend",
    "WakeBackend",
    "WakeDetector",
    "create_wake_detector",
    "matches_wake_word",
    "normalize_wake_text",
    "strip_wake_word",
    "vosk_available",
    "vosk_model_path",
    "wake_pattern",
    "wake_stems",
]