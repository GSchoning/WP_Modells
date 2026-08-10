"""Free port 8000: kill any running GABORA server processes.

The container image has no lsof/ps/pkill, so this walks /proc directly.
Run when `serve` dies with "[Errno 98] address already in use":

    python scripts/kill_server.py
"""
from __future__ import annotations

import os
import signal
import sys
import time
import urllib.request


def main() -> int:
    me = os.getpid()
    killed = []
    for pid in (p for p in os.listdir("/proc") if p.isdigit()):
        if int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if ("python" in cmd and "src.cli" in cmd and "serve" in cmd) or "uvicorn" in cmd:
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed.append((pid, cmd.strip()[:90]))
            except OSError:
                pass
    for pid, cmd in killed:
        print(f"killed {pid}: {cmd}")
    if not killed:
        print("no server processes found")
    time.sleep(1.5)
    try:
        urllib.request.urlopen("http://localhost:8000/api/healthz", timeout=2)
        print("WARNING: port 8000 is still answering — something else holds it")
        return 1
    except Exception:
        print("port 8000 is free")
        return 0


if __name__ == "__main__":
    sys.exit(main())
