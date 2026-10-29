"""PyInstaller entry point for the VoiceRunaCore sidecar.

PyInstaller runs the entry script as a top-level __main__ module, so the
sidecar package entry point (sidecar/voiceruna/__main__.py) cannot be used
directly: its relative imports (``from . import __version__``) would fail with
"attempted relative import with no known parent package".

This launcher imports the real entry point as part of the voiceruna package and
calls it. The package itself is found through pathex=<repo>/sidecar in
voiceruna-sidecar.spec.
"""

from __future__ import annotations

import sys

from voiceruna.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
