"""Text normalization for the fast path (TZ §6).

Lower case, ё -> е, punctuation removed, then the leading wake word and
command words are dropped: «ассистент, открой доту» -> «доту». The file template
keeps dots so a file name like «отчёт.pdf» survives normalization.
"""

from __future__ import annotations

import re

__all__ = ["LEADING_WORDS", "normalize", "normalize_keep_dots"]

# Removed only from the beginning of the phrase, in any order.
LEADING_WORDS = frozenset(
    {
        "вокс",
        "vox",
        "открой",
        "откройка",
        "открыть",
        "запусти",
        "запустить",
        "включи",
        "включить",
        "вруби",
        "покажи",
        "показать",
        "будь",
        "добр",
        "добрый",
        "ка",
        "мне",
        "скажи",
        "подскажи",
        "пожалуйста",
    }
)

# Removed from the end of the phrase.
TRAILING_WORDS = frozenset({"пожалуйста", "плиз", "пж"})

_SPACES_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё\s]+")
_NON_WORD_DOTS_RE = re.compile(r"[^0-9a-zа-яё.\s]+")


def normalize(text: str) -> str:
    """Return the canonical form used for aliases, fuzzy matching and templates."""
    return _normalize(text, keep_dots=False, keep_yo=False)


def normalize_keep_dots(text: str) -> str:
    """Same as normalize() but keeps '.' and 'ё' so file names survive as spoken."""
    return _normalize(text, keep_dots=True, keep_yo=True)


def _normalize(text: str, *, keep_dots: bool, keep_yo: bool) -> str:
    if not isinstance(text, str):
        return ""
    value = text.casefold()
    if not keep_yo:
        value = value.replace("ё", "е")
    value = (_NON_WORD_DOTS_RE if keep_dots else _NON_WORD_RE).sub(" ", value)
    value = _SPACES_RE.sub(" ", value).strip()
    if not value:
        return ""
    tokens = value.split(" ")
    while tokens and tokens[0] in LEADING_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1] in TRAILING_WORDS:
        tokens.pop()
    return " ".join(tokens).strip()
