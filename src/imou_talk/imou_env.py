#!/usr/bin/env python3
"""Minimal .env loader (stdlib only, no python-dotenv dependency).

Python entry points here historically only read ``os.environ``, while the
PowerShell wrappers parsed the repo ``.env`` file. That meant running::

    python3 -u src/imou_talk/imou_rtsp_listen.py --ip ... --vu

directly on Linux always ended with an empty password (``rtsp://admin:@...``)
and ``401 Unauthorized``, even when ``.env`` was correct.

Call :func:`load_repo_dotenv` at the start of ``parse_args()``/``main()``
so direct ``python`` invocations behave like the ``.ps1`` wrappers:

* looks for ``.env`` in ``cwd`` first, then repo root
* ``setdefault`` semantics: real environment variables and CLI flags win
* strips surrounding single/double quotes (``IMOU_PASSWORD="abc"`` works)
* supports optional ``export KEY=...`` prefix, skips blanks/comments
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not _KEY_RE.match(key):
            continue
        # Strip matching surrounding quotes, same as the .ps1 wrappers.
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        values[key] = val
    return values


def load_repo_dotenv(dotenv_path: str | Path | None = None) -> Path | None:
    """Load ``.env`` into ``os.environ`` (without overriding existing vars)."""
    candidates: list[Path] = []
    if dotenv_path:
        candidates.append(Path(dotenv_path))
    else:
        candidates.append(Path.cwd() / ".env")
        repo_root = Path(__file__).resolve().parent.parent.parent
        if repo_root / ".env" not in candidates:
            candidates.append(repo_root / ".env")
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
        except OSError:
            continue
        for key, val in _parse_dotenv_file(cand).items():
            os.environ.setdefault(key, val)
        return cand
    return None
