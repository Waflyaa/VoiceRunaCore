"""Validators for tool arguments: folders, filenames, paths, URIs and .lnk targets.

Implements TZ §8.3, §9 (paths with %VAR%) and §14 (security). The rules are:

* a folder id must come from 'folders.allowed';
* a file name is a plain name: no '..', no separators, no absolute path,
  none of the characters \\ / : * ? " < > |, no control characters;
* the resolved path must stay strictly inside the resolved known folder, so a
  symlink or junction cannot escape the sandbox;
* the extension (lower case) must be listed in 'files.allowed_extensions';
* .lnk files are accepted only from 'files.lnk_allowed_folders' and their
  target is read from the Shell Link binary (MS-SHLLINK LinkInfo); targets
  that are scripts or live in a system folder are refused.

Every refusal raises ValidationError with a short Russian reason; the tool
layer turns that into a spoken refusal. No shell is ever invoked here.
"""

from __future__ import annotations

import os
import re
import shlex
import struct
from pathlib import Path

__all__ = [
    "ValidationError",
    "BLOCKED_LNK_TARGET_EXTENSIONS",
    "EXECUTABLE_SUFFIXES",
    "FORBIDDEN_FILENAME_CHARS",
    "known_folder",
    "normalize_alias",
    "validate_folder_id",
    "validate_filename",
    "validate_lnk_target",
    "validate_uri",
    "resolve_file",
    "expand_app_command",
]


class ValidationError(Exception):
    """Raised when a tool argument breaks a security rule of TZ §8.3/§14."""


# Characters Windows forbids inside a file name.
FORBIDDEN_FILENAME_CHARS = frozenset('\\/:*?"<>|')

# Reserved DOS device names: opening them can block or hijack a stream.
_RESERVED_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"})
_RESERVED_DEVICE_RE = re.compile(r"^(?:COM|LPT)[1-9]$")

# Extensions a shortcut must never point at (TZ §8.3, §14).
BLOCKED_LNK_TARGET_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1"})

# Suffixes that make a whitespace-separated token an executable path.
EXECUTABLE_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".msi", ".lnk", ".appref-ms")

_FOLDER_IDS = ("desktop", "downloads", "documents")

_FALLBACK_FOLDER_NAMES = {
    "desktop": "Desktop",
    "downloads": "Downloads",
    "documents": "Documents",
}

# Registry values in HKCU\...\Explorer\User Shell Folders (supports redirected
# and OneDrive folders without hard-coding paths).
_SHELL_FOLDER_VALUES = {
    "desktop": ("Desktop",),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}",),
    "documents": ("Personal",),
}
_SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"

# Shell Link (.lnk) binary layout, MS-SHLLINK.
_LNK_HEADER_SIZE = 0x4C
_LNK_CLSID = bytes.fromhex("0114020000000000c000000000000046")
_LNK_FLAG_HAS_LINK_INFO = 0x00000002
_LNK_FLAG_HAS_ID_LIST = 0x00000001
_LINKINFO_FLAG_VOLUME_ID_AND_LOCAL_BASE_PATH = 0x00000001


# --------------------------------------------------------------------------- aliases
def normalize_alias(text: str) -> str:
    """Normalize an alias or a spoken phrase: lower case, ё -> е, one space."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.lower().replace("ё", "е").split())


# --------------------------------------------------------------------------- folders
def known_folder(folder_id: str) -> Path:
    """Return the user's desktop/downloads/documents folder."""
    if folder_id not in _FOLDER_IDS:
        raise ValidationError(f"папка не разрешена: {folder_id}")
    redirected = _shell_folder(folder_id)
    if redirected is not None:
        return redirected
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(profile) / _FALLBACK_FOLDER_NAMES[folder_id]


def _shell_folder(folder_id: str) -> Path | None:
    """Read the folder from the Explorer registry; None when unavailable."""
    try:
        import winreg  # Windows only; imported lazily so the module stays importable
    except ImportError:  # pragma: no cover - non-Windows
        return None
    for value_name in _SHELL_FOLDER_VALUES[folder_id]:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY) as key:
                raw, _kind = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if not isinstance(raw, str) or not raw.strip():
            continue
        return Path(os.path.expandvars(raw.strip()))
    return None


def validate_folder_id(folder_id: str, allowed: list[str]) -> str:
    """Return the normalized folder id when it is in the allow-list."""
    if not isinstance(folder_id, str):
        raise ValidationError("folder_id должен быть строкой")
    candidate = folder_id.strip().lower()
    allowed_ids = {str(item).strip().lower() for item in (allowed or [])}
    if candidate not in allowed_ids:
        raise ValidationError(f"папка не разрешена: {folder_id}")
    return candidate


# --------------------------------------------------------------------------- file names
def validate_filename(filename: str) -> str:
    """Accept only a plain file name (TZ §8.3); returns it stripped."""
    if not isinstance(filename, str):
        raise ValidationError("имя файла должно быть строкой")
    name = filename.strip()
    if not name:
        raise ValidationError("пустое имя файла")
    if ".." in name:
        raise ValidationError("недопустимое имя файла: содержит ..")
    for char in name:
        if char in FORBIDDEN_FILENAME_CHARS:
            raise ValidationError(f"недопустимый символ в имени файла: {char}")
        if ord(char) < 32 or ord(char) == 127:
            raise ValidationError("недопустимый символ в имени файла")
    if name in {".", ".."}:
        raise ValidationError("недопустимое имя файла")
    if name.endswith((".", " ")):
        raise ValidationError("недопустимое имя файла: точка или пробел в конце")
    if Path(name).is_absolute() or re.match(r"^[A-Za-z]:", name):
        raise ValidationError("абсолютный путь запрещён")
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_DEVICE_NAMES or _RESERVED_DEVICE_RE.match(stem):
        raise ValidationError("зарезервированное имя файла")
    return name


def resolve_file(
    folder_id: str,
    filename: str,
    *,
    allowed_folders: list[str],
    allowed_extensions: list[str],
    lnk_allowed_folders: list[str],
) -> Path:
    """Validate open_file arguments and return the resolved absolute path.

    Every rule of TZ §8.3 is enforced: folder allow-list, safe name, resolved
    path strictly inside the folder (symlinks/junctions cannot escape),
    allowed extension and inspected .lnk target.
    """
    folder = validate_folder_id(folder_id, allowed_folders)
    name = validate_filename(filename)

    extension = Path(name).suffix.lower()
    extensions = {str(item).strip().lower() for item in (allowed_extensions or []) if str(item).strip()}
    if not extension or extension not in extensions:
        raise ValidationError(f"расширение не разрешено: {extension or name}")

    base = _resolved(known_folder(folder))
    candidate = base / name
    resolved = _resolved(candidate)
    if resolved.parent != base or resolved == base:
        raise ValidationError("файл вне разрешённой папки")

    if extension == ".lnk":
        lnk_folders = {str(item).strip().lower() for item in (lnk_allowed_folders or []) if str(item).strip()}
        if folder not in lnk_folders:
            raise ValidationError("ярлыки разрешены только из рабочего стола")
        validate_lnk_target(resolved)

    return resolved


def _resolved(path: Path) -> Path:
    """Path.resolve() that turns filesystem quirks into a ValidationError."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError("путь не разрешён") from exc


# --------------------------------------------------------------------------- .lnk
def validate_lnk_target(path: Path) -> None:
    """Refuse a shortcut that points at a script or a system folder."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError("ярлык не читается") from exc
    target = _read_lnk_target(data)
    if not target:
        raise ValidationError("цель ярлыка не читается")

    target_path = Path(target)
    if target_path.suffix.lower() in BLOCKED_LNK_TARGET_EXTENSIONS:
        raise ValidationError("ярлык на скрипт запрещён")
    resolved = _resolved(target_path)
    for system_dir in _system_dirs():
        system_resolved = _resolved(system_dir)
        if resolved == system_resolved or system_resolved in resolved.parents:
            raise ValidationError("ярлык в системную папку запрещён")


def _system_dirs() -> list[Path]:
    candidates: list[Path] = []
    for name in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value))
    candidates.append(Path(r"C:\Windows"))
    candidates.append(Path(r"C:\Program Files"))
    candidates.append(Path(r"C:\Program Files (x86)"))
    return candidates


def _read_lnk_target(data: bytes) -> str | None:
    """Read the local target path from a Shell Link binary, or None."""
    if len(data) < _LNK_HEADER_SIZE + 0x1C:
        return None
    if struct.unpack_from("<I", data, 0)[0] != _LNK_HEADER_SIZE:
        return None
    if data[4:20] != _LNK_CLSID:
        return None
    flags = struct.unpack_from("<I", data, 20)[0]
    if not flags & _LNK_FLAG_HAS_LINK_INFO:
        return None

    position = _LNK_HEADER_SIZE
    if flags & _LNK_FLAG_HAS_ID_LIST:
        if len(data) < position + 2:
            return None
        id_list_size = struct.unpack_from("<H", data, position)[0]
        position += 2 + id_list_size

    if position + 0x1C > len(data):
        return None
    link_info_size = struct.unpack_from("<I", data, position)[0]
    link_info_header = struct.unpack_from("<I", data, position + 4)[0]
    if link_info_size < 0x1C or position + link_info_size > len(data):
        return None
    if link_info_header < 0x1C:
        return None

    link_info_flags = struct.unpack_from("<I", data, position + 8)[0]
    base_offset = struct.unpack_from("<I", data, position + 16)[0]
    suffix_offset = struct.unpack_from("<I", data, position + 24)[0]
    base_offset_unicode = 0
    suffix_offset_unicode = 0
    if link_info_header >= 0x24 and position + link_info_header <= len(data):
        base_offset_unicode = struct.unpack_from("<I", data, position + 0x1C)[0]
        suffix_offset_unicode = struct.unpack_from("<I", data, position + 0x20)[0]

    if not link_info_flags & _LINKINFO_FLAG_VOLUME_ID_AND_LOCAL_BASE_PATH:
        return None

    base = _read_wide_string(data, position, base_offset_unicode) or _read_ansi_string(
        data, position, base_offset
    )
    if not base:
        return None
    suffix = _read_wide_string(data, position, suffix_offset_unicode) or _read_ansi_string(
        data, position, suffix_offset
    )
    target = base + (suffix or "")
    return target or None


def _read_wide_string(data: bytes, base: int, offset: int) -> str | None:
    """Read a NUL-terminated UTF-16LE string located at base + offset."""
    if offset <= 0:
        return None
    start = base + offset
    if start + 2 > len(data):
        return None
    raw = bytearray()
    position = start
    while position + 1 < len(data):
        pair = data[position : position + 2]
        if pair == b"\x00\x00":
            break
        raw += pair
        position += 2
    if not raw:
        return None
    return raw.decode("utf-16-le", errors="replace")


def _read_ansi_string(data: bytes, base: int, offset: int) -> str | None:
    """Read a NUL-terminated ANSI string located at base + offset."""
    if offset <= 0:
        return None
    start = base + offset
    if start >= len(data):
        return None
    end = data.find(b"\x00", start)
    if end < 0:
        end = len(data)
    raw = data[start:end]
    if not raw:
        return None
    try:
        return raw.decode("mbcs")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("cp1251", errors="replace")


# --------------------------------------------------------------------------- URIs
def validate_uri(uri: str, allowed_schemes: list[str]) -> str:
    """Accept a URI whose scheme (before the colon) is allow-listed, ignoring case."""
    if not isinstance(uri, str):
        raise ValidationError("URI должен быть строкой")
    value = uri.strip()
    if not value:
        raise ValidationError("пустой URI")
    if any(ord(char) < 32 or char == " " for char in value):
        raise ValidationError("недопустимый URI")

    scheme, separator, _rest = value.partition(":")
    if not separator or not scheme:
        raise ValidationError("URI без схемы")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*", scheme):
        raise ValidationError("недопустимая схема URI")

    allowed = {
        str(item).strip().lower().rstrip(":")
        for item in (allowed_schemes or [])
        if str(item).strip()
    }
    if scheme.lower() not in allowed:
        raise ValidationError(f"схема не разрешена: {scheme}")
    return value


# --------------------------------------------------------------------------- app commands
def expand_app_command(entry: dict) -> tuple[str, list[str]]:
    """Expand %VAR% in an apps.json path and split it into exe + arguments.

    Windows paths may contain spaces, so the executable is the longest leading
    token run that exists on disk; otherwise the shortest run that ends with an
    executable suffix is taken and the rest becomes arguments.
    """
    if not isinstance(entry, dict):
        raise ValidationError("некорректная запись программы")
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValidationError("у программы не задан путь")
    expanded = os.path.expandvars(raw_path.strip())
    if not expanded:
        raise ValidationError("пустая команда программы")

    if Path(expanded).exists():
        return expanded, []

    if '"' in expanded:
        try:
            tokens = [token for token in shlex.split(expanded, posix=False) if token]
        except ValueError as exc:
            raise ValidationError("не удалось разобрать команду программы") from exc
        tokens = [_strip_quotes(token) for token in tokens]
        if not tokens:
            raise ValidationError("пустая команда программы")
        return tokens[0], tokens[1:]

    tokens = expanded.split()
    if not tokens:
        raise ValidationError("пустая команда программы")

    for index in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:index])
        if Path(candidate).exists():
            return candidate, tokens[index:]
    for index in range(1, len(tokens) + 1):
        candidate = " ".join(tokens[:index])
        if candidate.lower().endswith(EXECUTABLE_SUFFIXES):
            return candidate, tokens[index:]
    return tokens[0], tokens[1:]


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token
