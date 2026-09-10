#!/usr/bin/env python3
"""Start the addon (Granian in the foreground). Cross-platform.

Env knobs (all optional):
  ADDON_HOST           bind address (default 127.0.0.1)
  ADDON_PORT           port (default 7002)
  PROXY_ENABLED        0 = /proxy refuses, no video bytes via this box (default 0)
  INCLUDE_PROXY_FALLBACK  1 = also emit /proxy stream entries (default 0)
  AUDIO_TARGET_LUFS    normalize proxied audio here, 0 = off (default -24)
"""
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADDON_DIR = ROOT / "addon"

HOST = os.environ.get("ADDON_HOST", "127.0.0.1")
PORT = int(os.environ.get("ADDON_PORT", "7002"))


def is_up() -> bool:
    host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    try:
        socket.create_connection((host, PORT), timeout=2).close()
        return True
    except OSError:
        return False


def granian_cmd() -> list | None:
    exe = shutil.which("granian")
    if exe:
        return [exe]
    try:
        import granian  # noqa: F401  (pip package present -> python -m works)
        return [sys.executable, "-m", "granian"]
    except ImportError:
        return None


def main() -> int:
    if is_up():
        print(f"addon already up on :{PORT} - nothing to do")
        return 0
    os.environ.setdefault("PROXY_ENABLED", "0")
    base = granian_cmd()
    if base is None:
        print("granian not found: pip install -r addon/requirements.txt", file=sys.stderr)
        return 1
    cmd = base + [
        "--interface", "asgi", "app.main:app",
        "--host", HOST, "--port", str(PORT), "--workers", "1",
    ]
    proc = subprocess.Popen(cmd, cwd=ADDON_DIR)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C: stop the server, then exit.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
