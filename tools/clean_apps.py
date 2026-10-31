"""Remove games from the user's apps.json (dota2/factorio/steam rungameid).

Run:  python tools/clean_apps.py
The registry keeps a backup as apps.json.bak before rewriting.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

from voiceruna import paths  # noqa: E402
from voiceruna.apps import AppsRegistry  # noqa: E402


def main() -> int:
    registry = AppsRegistry(paths.apps_path())
    apps = registry.load()
    print(f"apps.json: {paths.apps_path()}")
    print("entries:", ", ".join(apps) or "(empty)")
    if Path(str(paths.apps_path()) + ".bak").exists():
        print("backup:", str(paths.apps_path()) + ".bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
