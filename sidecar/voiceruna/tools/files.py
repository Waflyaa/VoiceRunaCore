"""create_file: text files only, strictly inside %USERPROFILE%\\Documents\\VoiceRunaCore.

The tool takes a bare file name (never a path), so "создай файл в C:\\Windows"
is refused: there is no argument that could point outside the sandbox, and the
resolved path is checked again before writing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = ["SANDBOX_NAME", "create_text_file", "sandbox_dir"]

#: Folder under Documents where the assistant may create files.
SANDBOX_NAME = "VoiceRunaCore"
ALLOWED_SUFFIXES = (".txt", ".md")
MAX_NAME_LENGTH = 80
MAX_CONTENT_LENGTH = 20_000

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sandbox_dir() -> Path:
    """%USERPROFILE%\\Documents\\VoiceRunaCore (created on demand)."""
    home = os.environ.get("USERPROFILE") or str(Path.home())
    path = Path(home) / "Documents" / SANDBOX_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_name(filename: str) -> str:
    name = (filename or "").strip().strip('"').strip("'")
    if not name:
        raise ValueError("не задано имя файла")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("слишком длинное имя файла")
    if _BAD_NAME.search(name) or "/" in name or "\\" in name or ".." in name:
        raise ValueError("в имени файла есть недопустимые символы")
    stem = name.rsplit(".", 1)[0].strip().lower()
    if not stem or stem in _RESERVED:
        raise ValueError("недопустимое имя файла")
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise ValueError("можно создавать только .txt и .md")
    return name


def create_text_file(filename: str, content: str | None = None) -> Path:
    """Create a .txt/.md file inside the sandbox; refuses to overwrite."""
    name = _validate_name(filename)
    text = "" if content is None else str(content)
    if len(text) > MAX_CONTENT_LENGTH:
        raise ValueError("слишком длинный текст для файла")
    base = sandbox_dir().resolve()
    path = (base / name).resolve()
    if path.parent != base or not base.is_dir():
        raise ValueError("файл можно создать только в папке VoiceRunaCore")
    if path.exists():
        raise ValueError(f"файл уже существует: {name}")
    path.write_text(text, encoding="utf-8")
    return path
