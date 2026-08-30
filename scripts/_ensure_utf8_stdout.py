"""scripts/_ensure_utf8_stdout.py — Windows-console safety shim.

Several scripts print strings that may contain non-ASCII characters (the
rupee sign ₹ in module docstrings of imported modules, e.g. funding.py,
typologies.py, the bullet "→", etc.). On a Windows console whose code page
is cp1252 (the default), printing such strings raises `UnicodeEncodeError`
mid-run. This shim reconfigures sys.stdout (and sys.stderr) to utf-8 with
the `replace` error handler so the scripts run end-to-end on Windows.

Idempotent: safe to call multiple times. No-ops on Python < 3.7 (which
lacks TextIOWrapper.reconfigure). Imported by every script under scripts/
as the first non-stdlib import.
"""
from __future__ import annotations

import sys


def ensure_utf8_stdout() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Never let the shim itself crash the run; worst case the
            # stream keeps its current encoding.
            pass
