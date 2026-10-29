"""Tests for the security validators (TZ §8.3, §14).

The path tests run on tmp_path with a monkeypatched known_folder, so no real
user folder is touched. No shell and no network are involved.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from voiceruna import security
from voiceruna.security import ValidationError

CLSID = bytes.fromhex("0114020000000000c000000000000046")
ALLOWED_EXTENSIONS = [".txt", ".lnk"]


def build_lnk(base_path: str, *, suffix: str = "", unicode_strings: bool = True) -> bytes:
    """Build a minimal Shell Link (.lnk) with a LinkInfo local base path."""
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)
    header[4:20] = CLSID
    struct.pack_into("<I", header, 20, 0x02)  # HasLinkInfo

    if unicode_strings:
        header_size = 0x24
        base_bytes = base_path.encode("utf-16-le") + b"\x00\x00"
        suffix_bytes = suffix.encode("utf-16-le") + b"\x00\x00" if suffix else b""
        base_offset = header_size
        suffix_offset = header_size + len(base_bytes) if suffix else 0
        link_info_size = header_size + len(base_bytes) + len(suffix_bytes)
        link_info = bytearray(header_size)
        struct.pack_into("<I", link_info, 0, link_info_size)
        struct.pack_into("<I", link_info, 4, header_size)
        struct.pack_into("<I", link_info, 8, 0x01)  # VolumeIDAndLocalBasePath
        struct.pack_into("<I", link_info, 12, 0)  # VolumeIDOffset
        struct.pack_into("<I", link_info, 16, 0)  # LocalBasePathOffset
        struct.pack_into("<I", link_info, 20, 0)  # CommonNetworkRelativeLinkOffset
        struct.pack_into("<I", link_info, 24, 0)  # CommonPathSuffixOffset
        struct.pack_into("<I", link_info, 0x1C, base_offset)
        struct.pack_into("<I", link_info, 0x20, suffix_offset)
        return bytes(header) + bytes(link_info) + base_bytes + suffix_bytes

    header_size = 0x1C
    base_bytes = base_path.encode("mbcs") + b"\x00"
    suffix_bytes = suffix.encode("mbcs") + b"\x00" if suffix else b""
    base_offset = header_size
    suffix_offset = header_size + len(base_bytes) if suffix else 0
    link_info_size = header_size + len(base_bytes) + len(suffix_bytes)
    link_info = bytearray(header_size)
    struct.pack_into("<I", link_info, 0, link_info_size)
    struct.pack_into("<I", link_info, 4, header_size)
    struct.pack_into("<I", link_info, 8, 0x01)
    struct.pack_into("<I", link_info, 16, base_offset)
    struct.pack_into("<I", link_info, 24, suffix_offset)
    return bytes(header) + bytes(link_info) + base_bytes + suffix_bytes


@pytest.fixture()
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    mapping = {
        "desktop": tmp_path / "Desktop",
        "downloads": tmp_path / "Downloads",
        "documents": tmp_path / "Documents",
    }
    for path in mapping.values():
        path.mkdir()
    monkeypatch.setattr(security, "known_folder", lambda folder_id: mapping[folder_id])
    return mapping


def resolve(
    folder_id: str,
    filename: str,
    *,
    allowed_folders: tuple[str, ...] = ("desktop", "downloads", "documents"),
    allowed_extensions: tuple[str, ...] = tuple(ALLOWED_EXTENSIONS),
    lnk_allowed_folders: tuple[str, ...] = ("desktop",),
) -> Path:
    return security.resolve_file(
        folder_id,
        filename,
        allowed_folders=list(allowed_folders),
        allowed_extensions=list(allowed_extensions),
        lnk_allowed_folders=list(lnk_allowed_folders),
    )


# --------------------------------------------------------------------------- filenames
@pytest.mark.parametrize(
    "filename",
    [
        "",
        "   ",
        "..",
        "../note.txt",
        r"..\note.txt",
        r"C:\Windows\note.txt",
        r"\note.txt",
        "/note.txt",
        "a/b.txt",
        r"a\b.txt",
        'a"b.txt',
        "a|b.txt",
        "a<b.txt",
        "a>b.txt",
        "a*b.txt",
        "a?b.txt",
        "a:b.txt",
        "file.txt.",
        "CON.txt",
        "com1.txt",
        "bad\x00name.txt",
    ],
)
def test_validate_filename_rejects(filename: str) -> None:
    with pytest.raises(ValidationError):
        security.validate_filename(filename)


@pytest.mark.parametrize(
    "filename",
    ["note.txt", "отчёт.pdf", "my file.docx", "a-b_c.txt", "Фото 2024.JPG"],
)
def test_validate_filename_accepts(filename: str) -> None:
    assert security.validate_filename(filename) == filename


def test_validate_folder_id() -> None:
    assert security.validate_folder_id("desktop", ["desktop", "downloads"]) == "desktop"
    assert security.validate_folder_id(" Desktop ", ["desktop"]) == "desktop"
    with pytest.raises(ValidationError):
        security.validate_folder_id("downloads", ["desktop"])
    with pytest.raises(ValidationError):
        security.validate_folder_id(None, ["desktop"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- resolve_file
def test_resolve_file_accepts_file_in_folder(folders: dict[str, Path]) -> None:
    target = folders["desktop"] / "note.txt"
    target.write_text("hello", encoding="utf-8")

    assert resolve("desktop", "note.txt") == target.resolve()


def test_resolve_file_accepts_uppercase_extension(folders: dict[str, Path]) -> None:
    target = folders["desktop"] / "NOTE.TXT"
    target.write_text("hello", encoding="utf-8")

    resolved = resolve("desktop", "NOTE.TXT")

    assert resolved.parent == folders["desktop"].resolve()
    assert resolved.name.lower() == "note.txt"


@pytest.mark.parametrize(
    ("folder_id", "filename"),
    [
        ("downloads", "../note.txt"),
        ("desktop", r"C:\Windows\note.txt"),
        ("desktop", "bad?.txt"),
        ("desktop", ""),
    ],
)
def test_resolve_file_rejects_bad_name(folders: dict[str, Path], folder_id: str, filename: str) -> None:
    with pytest.raises(ValidationError):
        resolve(folder_id, filename)


def test_resolve_file_rejects_folder_outside_allow_list(folders: dict[str, Path]) -> None:
    with pytest.raises(ValidationError):
        resolve("downloads", "note.txt", allowed_folders=("desktop",))


def test_resolve_file_rejects_extension(folders: dict[str, Path]) -> None:
    (folders["desktop"] / "app.exe").write_bytes(b"")
    with pytest.raises(ValidationError):
        resolve("desktop", "app.exe")
    with pytest.raises(ValidationError):
        resolve("desktop", "noextension")


def test_resolve_file_rejects_escape_through_symlink(folders: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("secret", encoding="utf-8")
    link = folders["desktop"] / "link.txt"
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this machine")

    with pytest.raises(ValidationError):
        resolve("desktop", "link.txt")


def test_resolve_file_rejects_lnk_outside_desktop(folders: dict[str, Path], tmp_path: Path) -> None:
    target = str(tmp_path / "safe.exe")
    lnk = folders["downloads"] / "shortcut.lnk"
    lnk.write_bytes(build_lnk(target))

    with pytest.raises(ValidationError):
        resolve("downloads", "shortcut.lnk")


def test_resolve_file_rejects_lnk_to_script(folders: dict[str, Path], tmp_path: Path) -> None:
    lnk = folders["desktop"] / "run.lnk"
    lnk.write_bytes(build_lnk(str(tmp_path / "evil.bat")))

    with pytest.raises(ValidationError):
        resolve("desktop", "run.lnk")


def test_resolve_file_rejects_lnk_to_system_folder(folders: dict[str, Path]) -> None:
    lnk = folders["desktop"] / "system.lnk"
    lnk.write_bytes(build_lnk(r"C:\Windows\System32\cmd.exe"))

    with pytest.raises(ValidationError):
        resolve("desktop", "system.lnk")


def test_resolve_file_rejects_broken_lnk(folders: dict[str, Path]) -> None:
    lnk = folders["desktop"] / "broken.lnk"
    lnk.write_bytes(b"definitely not a shell link" * 10)

    with pytest.raises(ValidationError):
        resolve("desktop", "broken.lnk")


def test_resolve_file_accepts_safe_lnk(folders: dict[str, Path], tmp_path: Path) -> None:
    target = tmp_path / "user app.exe"
    lnk = folders["desktop"] / "app.lnk"
    lnk.write_bytes(build_lnk(str(target)))

    assert resolve("desktop", "app.lnk") == lnk.resolve()


# --------------------------------------------------------------------------- lnk parsing
def test_lnk_unicode_target(tmp_path: Path) -> None:
    lnk = tmp_path / "u.lnk"
    lnk.write_bytes(build_lnk(str(tmp_path / "app.exe")))

    assert security._read_lnk_target(lnk.read_bytes()) == str(tmp_path / "app.exe")


def test_lnk_ansi_target(tmp_path: Path) -> None:
    lnk = tmp_path / "a.lnk"
    lnk.write_bytes(build_lnk(str(tmp_path / "app.exe"), unicode_strings=False))

    assert security._read_lnk_target(lnk.read_bytes()) == str(tmp_path / "app.exe")


def test_lnk_common_path_suffix(tmp_path: Path) -> None:
    lnk = tmp_path / "s.lnk"
    base = str(tmp_path / "Desktop") + "\\"
    lnk.write_bytes(build_lnk(base, suffix="app.exe"))

    assert security._read_lnk_target(lnk.read_bytes()) == base + "app.exe"
    security.validate_lnk_target(lnk)


def test_validate_lnk_target_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        security.validate_lnk_target(tmp_path / "missing.lnk")


@pytest.mark.parametrize("target", [r"C:\Windows\System32\cmd.exe", r"C:\Program Files\App\app.exe"])
def test_validate_lnk_target_rejects_system_dirs(tmp_path: Path, target: str) -> None:
    lnk = tmp_path / "t.lnk"
    lnk.write_bytes(build_lnk(target))

    with pytest.raises(ValidationError):
        security.validate_lnk_target(lnk)


@pytest.mark.parametrize("name", ["run.bat", "run.ps1", "run.cmd", "run.BAT"])
def test_validate_lnk_target_rejects_scripts(tmp_path: Path, name: str) -> None:
    lnk = tmp_path / "t.lnk"
    lnk.write_bytes(build_lnk(str(tmp_path / name)))

    with pytest.raises(ValidationError):
        security.validate_lnk_target(lnk)


# --------------------------------------------------------------------------- URIs
@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("steam://rungameid/570", ["steam:"]),
        ("STEAM://rungameid/570", ["steam:"]),
        ("steam://rungameid/570", ["STEAM:"]),
        ("steam://rungameid/570", ["steam"]),
    ],
)
def test_validate_uri_accepts(uri: str, allowed: list[str]) -> None:
    assert security.validate_uri(uri, allowed) == uri


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("http://example.com", ["steam:"]),
        ("javascript:alert(1)", ["steam:"]),
        ("file:///C:/Windows", ["steam:"]),
        ("no-scheme", ["steam:"]),
        ("", ["steam:"]),
        ("steam://run game", ["steam:"]),
        ("1steam://run", ["steam:"]),
    ],
)
def test_validate_uri_rejects(uri: str, allowed: list[str]) -> None:
    with pytest.raises(ValidationError):
        security.validate_uri(uri, allowed)


# --------------------------------------------------------------------------- app commands
def test_expand_app_command_bare_executable() -> None:
    assert security.expand_app_command({"path": "notepad.exe"}) == ("notepad.exe", [])


def test_expand_app_command_existing_path_with_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "My App"
    directory.mkdir()
    executable = directory / "app.exe"
    executable.write_bytes(b"")

    command, args = security.expand_app_command({"path": f"{executable} --silent"})

    assert command == str(executable)
    assert args == ["--silent"]


def test_expand_app_command_quoted_command(tmp_path: Path) -> None:
    directory = tmp_path / "My App"
    directory.mkdir()
    executable = directory / "app.exe"
    executable.write_bytes(b"")

    command, args = security.expand_app_command({"path": f'"{executable}" --flag value'})

    assert command == str(executable)
    assert args == ["--flag", "value"]


def test_expand_app_command_expands_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOX_TEST_APPS_DIR", str(tmp_path))
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"")

    command, args = security.expand_app_command({"path": "%VOX_TEST_APPS_DIR%\\tool.exe --flag"})

    assert command == str(executable)
    assert args == ["--flag"]


def test_expand_app_command_keeps_unknown_variable(tmp_path: Path) -> None:
    command, args = security.expand_app_command({"path": "%VOX_UNKNOWN_VAR%\\ghost.exe"})

    assert command.endswith("ghost.exe")
    assert args == []


def test_expand_app_command_rejects_uri_entry() -> None:
    with pytest.raises(ValidationError):
        security.expand_app_command({"uri": "steam://rungameid/570"})


# --------------------------------------------------------------------------- known folders
def test_known_folder_returns_absolute_path() -> None:
    path = security.known_folder("desktop")
    assert isinstance(path, Path)
    assert path.is_absolute()


def test_known_folder_rejects_unknown_id() -> None:
    with pytest.raises(ValidationError):
        security.known_folder("music")
