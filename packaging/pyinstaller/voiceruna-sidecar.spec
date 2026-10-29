# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir-сборка Python-сайдкара VoiceRunaCore (TZ §3).

Почему onedir, а не onefile:
  * onefile распаковывается в %TEMP% на каждом старте — 2-4 с холодного старта;
  * Tauri externalBin ждёт ОДИН файл, а нам нужен exe + папка _internal.
    Поэтому папка целиком кладётся в bundle.resources как "sidecar/", и Rust
    запускает resource_dir()/sidecar/voiceruna-sidecar.exe.

Результат (при --distpath packaging/dist):
    packaging/dist/sidecar/voiceruna-sidecar.exe
    packaging/dist/sidecar/_internal/...

Запуск — только через packaging/build-sidecar.ps1: он поднимает venv, ставит
зависимости и передаёт --distpath/--workpath, а также VOX_REPO_ROOT.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def _repo_root() -> Path:
    """Корень репозитория: env от build-sidecar.ps1, иначе SPECPATH, иначе cwd."""
    env_root = os.environ.get("VOX_REPO_ROOT")
    if env_root:
        return Path(env_root).resolve()
    try:
        # SPECPATH = packaging/pyinstaller -> parents[1] = корень репозитория
        return Path(SPECPATH).resolve().parents[1]  # noqa: F821
    except NameError:
        return Path.cwd()


REPO_ROOT = _repo_root()
SIDECAR_SRC = REPO_ROOT / "sidecar"

# PyInstaller executes the entry script as a top-level __main__ module, so
# sidecar/voiceruna/__main__.py cannot be the entry: its relative imports fail
# with "attempted relative import with no known parent package". launcher.py
# imports voiceruna.__main__ as part of the voiceruna package and calls main().
SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821
ENTRY = SPEC_DIR / "launcher.py"

if not ENTRY.exists():
    raise SystemExit(
        "[spec] не найден launcher сайдкара: %s\n"
        "[spec] ожидается packaging/pyinstaller/launcher.py и запуск через packaging/build-sidecar.ps1"
        % ENTRY
    )

# --- скрытые импорты ------------------------------------------------------- #
# uvicorn подтягивает реализации через строки; без этого не находится в бандле.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

datas = []
binaries = []

# collect_all: данные (голоса edge-tts, ассеты whisper) + нативные DLL
# (onnxruntime, ctranslate2, vosk, libsndfile). Пакеты, которых нет в окружении,
# пропускаем: спека должна работать и в урезанном venv для тестов.
for package in (
    "uvicorn",
    "websockets",
    "wsproto",
    "fastapi",
    "starlette",
    "pydantic",
    "edge_tts",
    "rapidfuzz",
    "httpx",
    "httpcore",
    "anyio",
    "certifi",
    "sounddevice",
    "soundfile",
    "miniaudio",
    "vosk",
    "onnxruntime",
    "faster_whisper",
    "ctranslate2",
    # Local neural voice: the wheel ships espeakbridge.pyd and the whole
    # espeak-ng-data tree, so piper needs collect-all or synthesis dies in the
    # frozen app with "espeak-ng data not found".
    "piper",
    "pathvalidate",
):
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - спека не должна падать из-за мелочи
        print("[spec] %s пропущен: %s" % (package, exc))
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SIDECAR_SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # В установщик не тащим тестовую обвязку и GUI-тулкиты.
        "pytest",
        "pytest_asyncio",
        "_pytest",
        "tkinter",
        "matplotlib",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# console=True + CREATE_NO_WINDOW из Rust: у windowed-сборки ломается stdout/stderr,
# а uvicorn пишет туда лог. Окно консоли скрывает сама оболочка.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="voiceruna-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# name="sidecar" => dist/sidecar/voiceruna-sidecar.exe + dist/sidecar/_internal/
# ровно то, что ждёт bundle.resources в app/src-tauri/tauri.conf.json.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sidecar",
)
