"""Fast path router (TZ §6): exact alias -> fuzzy -> templates -> LLM.

The router makes no network calls and never touches the GPU. It only picks a
tool and arguments; the same validators as the LLM path run inside the tool
executor, so routing cannot bypass security. When the phrase is ambiguous
(two applications above the fuzzy threshold) or a template does not parse,
the result is tool=None and the runtime asks the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from ..security import normalize_alias
from .normalize import normalize, normalize_keep_dots

if TYPE_CHECKING:  # pragma: no cover
    from ..apps import AppsRegistry
    from ..config import ConfigManager

__all__ = ["RouteResult", "Router"]

DEFAULT_FUZZY_THRESHOLD = 0.85

TIME_RE = re.compile(
    r"^(?:который\s+(?:сейчас\s+)?час|сколько\s+(?:сейчас\s+)?времени|время|текущее\s+время)$"
)
WEATHER_RE = re.compile(r"\bпогод[аыуе]?\b")
CITY_RE = re.compile(r"\bв(?:о)?\s+([0-9a-zа-я][0-9a-zа-я\s]*)$")
FOLDER_RE = re.compile(r"^папк[ауи]?\s+(.+)$")
FILE_RE = re.compile(r"^файл\s+(.+?)\s+(?:в|во|из|на)\s+(.+)$")
VOLUME_RE = re.compile(
    r"^(?:"
    r"(?P<up>громче|прибавь\s+(?:громкость|звук)|увеличь\s+(?:громкость|звук))"
    r"|(?P<down>тише|убавь\s+(?:громкость|звук)|уменьши\s+(?:громкость|звук))"
    r"|(?P<mute>выключи\s+(?:звук|громкость)|без\s+звука)"
    r"|(?P<unmute>включи\s+(?:звук|громкость))"
    r"|(?P<set>громкость\s+(?P<level>\d{1,3})\s*%?)"
    r")$"
)
CREATE_FILE_RE = re.compile(
    r"^(?:создай|сделай|запиши)\s+(?:файл|заметку)\s+(?P<name>.+?)"
    r"(?:\s+(?:с\s+текстом|текстом)\s+(?P<content>.+))?$"
)

# Russian folder names -> folder ids.
FOLDER_WORDS: dict[str, str] = {
    "desktop": "desktop",
    "рабочий стол": "desktop",
    "рабочего стола": "desktop",
    "рабочем столе": "desktop",
    "рабочий": "desktop",
    "стол": "desktop",
    "downloads": "downloads",
    "загрузки": "downloads",
    "загрузок": "downloads",
    "загрузка": "downloads",
    "загрузку": "downloads",
    "загрузке": "downloads",
    "загрузках": "downloads",
    "documents": "documents",
    "документы": "documents",
    "документов": "documents",
    "документа": "documents",
    "документе": "documents",
    "документах": "documents",
    "документ": "documents",
}

# Words that are not part of a folder name inside «папка …» / «файл … в …».
FOLDER_STOPWORDS = frozenset({"в", "во", "на", "мой", "мои", "моя", "папка", "папку", "папке", "папки"})

# Words that are not part of a city name («погода в москве сейчас»).
CITY_STOPWORDS = frozenset({"сейчас", "сегодня", "завтра", "на", "улице", "улица"})

MAX_CITY_LENGTH = 100
_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё\s]+")
_NON_WORD_DOTS_RE = re.compile(r"[^0-9a-zа-яё.\s]+")
_SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RouteResult:
    """One routing decision: tool=None means 'send the phrase to the LLM'."""

    tool: str | None
    args: dict
    kind: str
    score: float = 0.0


class Router:
    """Rule-based fast path in front of the LLM."""

    def __init__(self, config: ConfigManager, apps: AppsRegistry) -> None:
        self._config = config
        self._apps = apps

    def route(self, text: str) -> RouteResult:
        """Return the routing decision for one phrase."""
        if not isinstance(text, str):
            return _no_match()
        normalized = normalize(text)
        if not normalized:
            return _no_match()

        match_keys = self._match_keys()
        exact = match_keys.get(normalized)
        if exact is not None:
            return RouteResult("launch_application", {"application_id": exact}, "alias", 1.0)

        fuzzy, ambiguous = self._fuzzy_match(normalized, match_keys)
        if ambiguous:
            return _no_match()
        if fuzzy is not None:
            return fuzzy

        for matcher in (self._match_time, self._match_weather, self._match_folder):
            result = matcher(normalized)
            if result is not None:
                return result
        # Volume and file commands are matched on a lightly cleaned phrase:
        # normalize() drops leading verbs («включи звук» -> «звук»), which would
        # erase exactly the verb these templates need.
        for matcher in (self._match_volume, self._match_create_file):
            result = matcher(text)
            if result is not None:
                return result
        return self._match_file(text)

    # ------------------------------------------------------------------ aliases
    def _match_keys(self) -> dict[str, str]:
        """Normalized match key -> app id: aliases, the id and the display name.

        Aliases cover the spoken forms from TZ §9; the id and display name make
        «запусти Factorio» and «открой dota2» work exactly as written in §2.1.
        """
        keys: dict[str, str] = {}
        for app_id in self._apps.ids():
            entry = self._apps.get(app_id) or {}
            candidates: list[str] = list(entry.get("aliases", []))
            candidates.append(app_id)
            candidates.append(str(entry.get("display_name") or ""))
            for candidate in candidates:
                key = normalize_alias(candidate)
                if key and key not in keys:
                    keys[key] = app_id
        return keys

    def _fuzzy_match(self, normalized: str, match_keys: dict[str, str]) -> tuple[RouteResult | None, bool]:
        """Best fuzzy candidate, or (None, True) when the phrase is ambiguous."""
        threshold = self._threshold()
        scores: dict[str, float] = {}
        for alias, app_id in match_keys.items():
            score = fuzz.ratio(normalized, alias) / 100.0
            if score > scores.get(app_id, 0.0):
                scores[app_id] = score

        candidates = sorted(
            ((score, app_id) for app_id, score in scores.items() if score >= threshold),
            reverse=True,
        )
        if len(candidates) >= 2:
            return None, True
        if candidates:
            score, app_id = candidates[0]
            return RouteResult("launch_application", {"application_id": app_id}, "fuzzy", score), False
        return None, False

    def _threshold(self) -> float:
        value = self._config.get("router.fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD)
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return DEFAULT_FUZZY_THRESHOLD
        if threshold > 1.0:
            threshold = threshold / 100.0
        return min(max(threshold, 0.0), 1.0)

    # ------------------------------------------------------------------ templates
    def _match_time(self, normalized: str) -> RouteResult | None:
        if TIME_RE.match(normalized):
            return RouteResult("get_time", {}, "time", 1.0)
        return None

    def _match_weather(self, normalized: str) -> RouteResult | None:
        if not WEATHER_RE.search(normalized):
            return None
        city = _city_from(normalized)
        if city is None:
            return RouteResult("get_weather", {}, "weather", 1.0)
        if len(city) > MAX_CITY_LENGTH:
            return None
        return RouteResult("get_weather", {"city": city}, "weather", 1.0)

    def _match_folder(self, normalized: str) -> RouteResult | None:
        match = FOLDER_RE.match(normalized)
        if not match:
            return None
        folder_id = folder_id_for(match.group(1))
        if folder_id is None:
            return None
        return RouteResult("open_folder", {"folder_id": folder_id}, "folder", 1.0)

    def _match_volume(self, text: str) -> RouteResult | None:
        match = VOLUME_RE.match(_clean_command(text, keep_dots=False))
        if not match:
            return None
        if match.group("set"):
            level = int(match.group("level"))
            if not 0 <= level <= 100:
                return None
            return RouteResult("set_volume", {"action": "set", "level": level}, "volume", 1.0)
        for action in ("up", "down", "mute", "unmute"):
            if match.group(action):
                return RouteResult("set_volume", {"action": action}, "volume", 1.0)
        return None

    def _match_create_file(self, text: str) -> RouteResult | None:
        match = CREATE_FILE_RE.match(_clean_command(text, keep_dots=True))
        if not match:
            return None
        filename = match.group("name").strip().strip('"').strip("'")
        if not filename:
            return None
        if "." not in filename:
            filename += ".txt"
        args: dict = {"filename": filename}
        content = match.group("content")
        if content:
            args["content"] = content.strip().strip('"').strip("'")
        return RouteResult("create_file", args, "create_file", 1.0)

    def _match_file(self, text: str) -> RouteResult:
        match = FILE_RE.match(normalize_keep_dots(text))
        if not match:
            return _no_match()
        filename = match.group(1).strip().strip('"').strip("'")
        folder_id = folder_id_for(match.group(2))
        if not filename or folder_id is None:
            return _no_match()
        return RouteResult("open_file", {"folder_id": folder_id, "filename": filename}, "file", 1.0)


#: Filler around a command: the wake word and politeness, never the verb.
_COMMAND_FILLER = frozenset(
    {"вокс", "vox", "ассистент", "ассистента", "будь", "добр", "добрый", "ка", "мне", "пожалуйста", "плиз", "пж"}
)


def _clean_command(text: str, *, keep_dots: bool) -> str:
    """Lower-case a phrase, drop the wake word, keep the leading verb."""
    if not isinstance(text, str):
        return ""
    value = text.casefold().replace("ё", "е")
    value = (_NON_WORD_DOTS_RE if keep_dots else _NON_WORD_RE).sub(" ", value)
    tokens = _SPACES_RE.sub(" ", value).strip().split()
    while tokens and tokens[0] in _COMMAND_FILLER:
        tokens.pop(0)
    while tokens and tokens[-1] in _COMMAND_FILLER:
        tokens.pop()
    return " ".join(tokens)


def _city_from(normalized: str) -> str | None:
    match = CITY_RE.search(normalized)
    if not match:
        return None
    words = match.group(1).split()
    while words and words[-1] in CITY_STOPWORDS:
        words.pop()
    if not words:
        return None
    return " ".join(words)


def folder_id_for(text: str) -> str | None:
    """Map a spoken folder name to desktop|downloads|documents."""
    words = [word for word in text.split() if word not in FOLDER_STOPWORDS]
    if not words:
        return None
    candidates = [" ".join(words)]
    if len(words) >= 2:
        candidates.append(" ".join(words[-2:]))
    candidates.append(words[-1])
    for candidate in candidates:
        folder_id = FOLDER_WORDS.get(candidate)
        if folder_id is not None:
            return folder_id
    return None


def _no_match() -> RouteResult:
    return RouteResult(None, {}, "none")
