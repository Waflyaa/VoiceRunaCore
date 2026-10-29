"""Sidecar entry point: uvicorn on 127.0.0.1 with a token from VOX_TOKEN.

The Tauri shell picks a free port and passes it as --port, and the shared secret
in the VOX_TOKEN environment variable (TZ §3).
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys

from . import __version__
from .config import ConfigManager
from .logging_setup import setup_logging


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="voiceruna-sidecar", description="VoiceRunaCore voice assistant sidecar")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (127.0.0.1 only by design)")
    parser.add_argument("--port", type=int, default=0, help="port to listen on, 0 picks a free one")
    parser.add_argument("--token", default="", help="auth token; defaults to VOX_TOKEN or a random value")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(__version__)
        return 0

    if args.host not in {"127.0.0.1", "localhost"}:
        print(f"refusing to bind {args.host}: the sidecar only listens on 127.0.0.1", file=sys.stderr)
        return 2

    token = args.token or os.environ.get("VOX_TOKEN") or secrets.token_hex(16)
    port = args.port or find_free_port(args.host)

    config = ConfigManager()
    config.load()
    setup_logging(config)

    import uvicorn

    from .app import create_app

    app = create_app(token=token)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=port, log_level="warning", access_log=False)
    )
    app.state.on_shutdown = lambda: setattr(server, "should_exit", True)

    # Printed for the shell logs; the token itself is never logged.
    print(f"voiceruna-sidecar {__version__} listening on http://{args.host}:{port}", flush=True)
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
