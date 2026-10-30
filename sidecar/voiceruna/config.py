"""Configuration store for the sidecar (TZ §12).

Layout on disk: %APPDATA%\\VoiceRunaCore\\config.json for settings and
.env for API keys. Keys never enter config.json or the repository.

Access uses dotted paths: cfg.get("llm.timeout_sec"), cfg.set("tts.rate", "+15%").
Writes are atomic (temp file + os.replace) so a crash cannot corrupt the config.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import paths

_MISSING = object()

log = logging.getLogger(__name__)

#: Characters that only appear when UTF-8 Russian was decoded as a single-byte
#: code page (single "Ð Ð¡" or double "Р В РЎ" mojibake). A healthy Russian
#: phrase never contains them, so they are safe repair markers.
_MOJIBAKE_MARKERS = (
    "\u00a0",
    "Ð",
    "Ñ",
    "\u0402",
    "\u0403",
    "\u0404",
    "\u0405",
    "\u0406",
    "\u0407",
    "\u0408",
    "\u0409",
    "\u040a",
    "\u040b",
    "\u040c",
    "\u040d",
    "\u040e",
    "\u040f",
)

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def _looks_like_mojibake(value: str) -> bool:
    return isinstance(value, str) and any(marker in value for marker in _MOJIBAKE_MARKERS)


def _mojibake_bytes(text: str) -> bytes:
    """Best-effort single-byte view of a mojibake string.

    Undefined code-page bytes surface as C1 control characters (U+0080..U+009F)
    which cannot be encoded back with cp1251; keeping every character below
    U+0100 as its own byte restores those losses.
    """
    out = bytearray()
    for char in text:
        code = ord(char)
        if code < 0x100:
            out.append(code)
            continue
        encoded = char.encode("cp1251")
        if len(encoded) != 1:  # pragma: no cover - cp1251 is single-byte
            raise UnicodeEncodeError("cp1251", char, 0, 1, "not a single byte")
        out.extend(encoded)
    return bytes(out)


def _fix_mojibake(value: str) -> str | None:
    """Undo up to four layers of UTF-8-read-as-a-single-byte-code-page."""
    text = value
    for _ in range(4):
        try:
            fixed = _mojibake_bytes(text).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if fixed == text:
            break
        text = fixed
        if not _looks_like_mojibake(text):
            break
    if text == value or _looks_like_mojibake(text):
        return None
    if not any(char in _CYRILLIC for char in text.lower()):
        return None
    return text


def _repair_mojibake(node: Any) -> bool:
    """Walk the config tree and repair corrupted Russian strings in place."""
    changed = False
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, str) and _looks_like_mojibake(value):
                fixed = _fix_mojibake(value)
                if fixed is not None:
                    node[key] = fixed
                    changed = True
            elif isinstance(value, (dict, list)):
                changed = _repair_mojibake(value) or changed
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str) and _looks_like_mojibake(value):
                fixed = _fix_mojibake(value)
                if fixed is not None:
                    node[index] = fixed
                    changed = True
            elif isinstance(value, (dict, list)):
                changed = _repair_mojibake(value) or changed
    return changed

DEFAULTS: dict[str, Any] = {
    "llm": {
        "providers": [
            {
                "id": "openrouter",
                "enabled": True,
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "model": "mistralai/mistral-small-3.1-24b-instruct:free",
                "supports_tools": True,
                "priority": 1,
                "label": "OpenRouter (free)",
            },
            {
                "id": "deepseek",
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "model": "deepseek-chat",
                "supports_tools": True,
                "priority": 2,
                "label": "DeepSeek",
            },
        ],
        "timeout_sec": 60,
        "max_messages": 8,
        "reset_timeout_sec": 120,
        "system_prompt_extra": "",
    },
    "stt": {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "unload_after_idle_sec": 300,
        "language": "ru",
        # Accuracy over the TZ default (beam 1): short Russian commands are
        # recognised noticeably better with a small beam and a vocabulary hint.
        "beam_size": 3,
        # The vocabulary hint makes whisper echo the prompt on noise, so it is
        # off by default; the echo filter in stt.py stays as a safety net.
        "initial_prompt": "",
        "normalize_audio": True,
        "normalize_min_peak": 0.0002,
        # AGC: bring the phrase up to a fixed target peak, never more than this.
        "normalize_max_gain": 100.0,
        "agc_target_peak": 0.95,
        # Hard anti-hallucination gates (owner report 2026-10-28): whisper invents
        # commands on silence and background noise, and the router executed them.
        "min_peak_dbfs": -45.0,
        # Layered guard adapted from whisper-guard/Leon:
        # (no_speech > drop_no_speech AND avg_logprob < drop_logprob) drops a
        # segment, extreme values drop it alone, compression_ratio catches loops,
        # and a dense confident segment is rescued even if no_speech is high.
        "drop_no_speech": 0.6,
        "drop_logprob": -1.0,
        "extreme_no_speech": 0.85,
        "extreme_logprob": -1.5,
        "max_compression_ratio": 3.0,
        "sparse_min_sec": 10.0,
        "sparse_density": 1.0,
        "rescue_logprob": -0.5,
        "rescue_density": 2.0,
        "rescue_min_sec": 5.0,
        "min_confidence": 0.42,
    },
    "vad": {
        # Endpointing by real silence, not a fixed timer: a long sentence with
        # short pauses stays one utterance, the phrase ends after 1.5 s of quiet.
        "silence_ms": 1500,
        # Long commands are the point of v2: allow half a minute of speech.
        "max_utterance_sec": 30,
        "pre_buffer_ms": 700,
        # Extra silence appended after the detected end before Whisper sees the
        # phrase: a hard cut on the last consonant is a common cause of garbage
        # transcripts (owner report 2026-10-28).
        "tail_pad_ms": 500,
    },
    "wakeword": {
        # vad_stt: always-on VAD capture + Whisper decides whether the phrase
        # started with the wake word. Works with any microphone (the Vosk KWS
        # model misses far-field speech); "vosk" stays available for low CPU.
        "engine": "vad_stt",
        "phrase": "вокс",
        "threshold": 0.5,
        "listen_timeout_sec": 6,
        # After a spoken answer the microphone stays armed for this long: a
        # reply without the wake word continues the dialogue.
        "followup_window_sec": 5,
        "bargein_bump": 0.15,
        # If Vosk is selected but its model is missing/broken, fall back to VAD+STT.
        "fallback_to_stt": True,
    },
    "hotkey": {"activate": "Ctrl+Alt+V"},
    "tts": {
        # auto = edge-tts, а при сбое синтеза/воспроизведения — офлайн SAPI (TZ 5.4, 15).
        "engine": "auto",
        "voice": "ru-RU-DmitryNeural",
        "rate": "+10%",
        "max_chars": 0,
        "silero_speaker": "baya",
        "voices": [
            {"engine": "edge", "name": "ru-RU-DmitryNeural", "label": "Дмитрий (мужской)"},
            {"engine": "edge", "name": "ru-RU-SvetlanaNeural", "label": "Светлана (женский)"},
            {"engine": "edge", "name": "ru-RU-DariyaNeural", "label": "Дария (женский)"},
        ],
    },
    "router": {"fuzzy_threshold": 0.85},
    "apps": {"allowed_uri_schemes": ["steam:"]},
    "folders": {"allowed": ["desktop", "downloads", "documents"]},
    "files": {
        "allowed_extensions": [
            ".exe", ".lnk", ".txt", ".pdf", ".docx", ".xlsx", ".png", ".jpg",
        ],
        "lnk_allowed_folders": ["desktop"],
    },
    "weather": {"default_city": "Сургут", "cache_ttl_sec": 600, "language": "ru"},
    "errors": {
        "stt_failed_phrase": "Не расслышал, повтори",
        "not_understood_phrase": "Я пока не умею это делать",
        "tool_refused_phrase": "не могу это сделать",
        "no_connection_phrase": "нет связи",
        "no_api_key_phrase": "не найден API ключ. Добавьте ключ в настройках, вкладка Модели",
        "auth_failed_phrase": "ключ не принят провайдером. Проверьте ключ в настройках",
        "mic_phrase": "микрофон недоступен",
    },
    "logging": {"enabled": False, "level": "info", "rotate_mb": 5},
    "audio": {
        "input_device": "default",
        "output_device": "default",
        # Per-device calibration: {"index:N": {"noise_dbfs": ..., "peak_dbfs": ...}}.
        # An empty dict marks the key as a free-form map that must survive reloads.
        "calibration": {},
    },
    # Diagnostics for "it does not hear me": the last recording lands in logs/.
    "debug": {"save_audio": False, "raw_seconds": 10},
    # Bumped when a default has to be migrated once; see _migrate().
    "config_version": 2,
}

# Sections whose values are plain dicts and therefore merged recursively.
_DICT_SECTIONS = {"llm", "stt", "vad", "wakeword", "hotkey", "tts", "router", "apps", "folders", "files", "weather", "errors", "logging", "audio", "debug"}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return base with patch applied recursively (patch wins)."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(default: Any, value: Any) -> Any:
    """Keep config types stable when a value arrives from the UI as JSON."""
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "да"}
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return value if isinstance(value, str) else str(value)
    return value


def _normalize_patch(patch: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Coerce incoming values to the type of the default they replace."""
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in template:
            continue
        default = template[key]
        if isinstance(default, dict) and isinstance(value, dict):
            if not default:
                # Free-form map (for example audio.calibration per device):
                # keep the entries, they are not described by the defaults.
                out[key] = copy.deepcopy(value)
            else:
                out[key] = _normalize_patch(value, default)
        elif default is not None and not isinstance(value, (dict, list)):
            out[key] = _coerce(default, value)
        else:
            out[key] = value
    return out


def _parse_dot(dotted: str) -> list[str]:
    return [part for part in dotted.split(".") if part]


class ConfigManager:
    """Thread-safe accessor for config.json plus the .env key store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.config_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._version = 0

    # ---------------------------------------------------------------- loading
    def load(self) -> dict[str, Any]:
        """Read config.json, filling missing keys from DEFAULTS; write back on first run."""
        with self._lock:
            raw: dict[str, Any] = {}
            created = False
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    raw = {}
            else:
                created = True
            if _repair_mojibake(raw):
                # An external tool (PowerShell round-trip) can mangle the
                # Russian phrases; repair them instead of speaking gibberish.
                log.info("config.json contained mojibake strings, repaired")
                created = True
            merged = _deep_merge(DEFAULTS, _normalize_patch(raw or {}, DEFAULTS))
            self._data = merged
            if self._migrate(raw or {}):
                created = True
            self._version += 1
            if created or merged != raw:
                self._write_locked()
            return copy.deepcopy(self._data)

    def _migrate(self, raw: dict[str, Any]) -> bool:
        """One-time migrations of old config files; returns True when changed.

        v1 -> v2: tts.engine was "edge" by default. offline SAPI fallback is now
        the sensible default ("auto"), so an untouched v1 file is upgraded.
        A later explicit choice in the settings editor is preserved, because the
        version is bumped after the first migration.
        """
        try:
            version = int(raw.get("config_version", 1) or 1)
        except (TypeError, ValueError):
            version = 1
        if version >= 2:
            return False
        tts = self._data.get("tts")
        if isinstance(tts, dict) and str(tts.get("engine", "")).lower() == "edge":
            tts["engine"] = "auto"
        self._data["config_version"] = 2
        return True

    def save(self) -> None:
        with self._lock:
            self._write_locked()
        self._notify()

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    # ---------------------------------------------------------------- access
    @property
    def version(self) -> int:
        return self._version

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        with self._lock:
            node: Any = self._data
            for part in _parse_dot(dotted):
                if not isinstance(node, dict) or part not in node:
                    if default is _MISSING:
                        raise KeyError(dotted)
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, dotted: str, value: Any, persist: bool = True) -> None:
        with self._lock:
            parts = _parse_dot(dotted)
            if not parts:
                raise KeyError(dotted)
            node: Any = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            template = self._template_for(parts)
            node[parts[-1]] = _coerce(template, value) if template is not _MISSING else value
            self._version += 1
            if persist:
                self._write_locked()
        if persist:
            self._notify()

    def update(self, patch: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        """Deep-merge a partial config (e.g. a PUT /config body)."""
        with self._lock:
            clean = _normalize_patch(patch, DEFAULTS)
            self._data = _deep_merge(self._data, clean)
            self._version += 1
            if persist:
                self._write_locked()
            result = copy.deepcopy(self._data)
        if persist:
            self._notify()
        return result

    def _template_for(self, parts: list[str]) -> Any:
        node: Any = DEFAULTS
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return _MISSING
            node = node[part]
        if isinstance(node, dict) and parts[-1] in node:
            return node[parts[-1]]
        return _MISSING

    # ---------------------------------------------------------------- events
    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
            snap = copy.deepcopy(self._data)
        for listener in listeners:
            try:
                listener(snap)
            except Exception:
                pass

    # ---------------------------------------------------------------- .env
    @staticmethod
    def env_path() -> Path:
        return paths.env_path()

    def env(self) -> dict[str, str]:
        """Parse .env into a dict (tolerates comments, quotes and BOM)."""
        path = self.env_path()
        result: dict[str, str] = {}
        if not path.exists():
            return result
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return result
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
        return result

    def env_value(self, name: str) -> str:
        """Look up a key: .env first, then the real process environment."""
        return self.env().get(name) or os.environ.get(name, "")

    def set_env_key(self, name: str, value: str) -> None:
        """Create or replace one key in .env, keeping other lines untouched."""
        path = self.env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        replaced = False
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith(name + "="):
                    lines.append(f"{name}={value}")
                    replaced = True
                else:
                    lines.append(line)
        if not replaced:
            lines.append(f"{name}={value}")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- helpers
    def enabled_providers(self, require_tools: bool = False) -> list[dict[str, Any]]:
        providers = self.get("llm.providers", [])
        if not isinstance(providers, list):
            return []
        result = []
        for provider in providers:
            if not isinstance(provider, dict) or not provider.get("enabled", True):
                continue
            if require_tools and not provider.get("supports_tools", True):
                continue
            result.append(provider)
        return sorted(result, key=lambda p: (p.get("priority", 100), str(p.get("id"))))

    def provider(self, provider_id: str) -> dict[str, Any] | None:
        for provider in self.get("llm.providers", []) or []:
            if isinstance(provider, dict) and provider.get("id") == provider_id:
                return provider
        return None
