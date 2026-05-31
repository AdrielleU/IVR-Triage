"""Load editable CSV data files with a simple mtime cache.

Files under data/ (contacts, hours, holidays) are read on demand and re-parsed
only when the file changes on disk — so users can edit/upload them and the next
call picks up the change, with no restart and no per-call re-parsing of large files.
"""

import logging
from pathlib import Path
from typing import Callable

from app.config import settings

log = logging.getLogger("ivr")

_cache: dict[str, tuple[float, object]] = {}


def data_path(name: str) -> Path:
    return Path(settings.data_dir) / name


def load_cached(name: str, parser: Callable[[Path], object]):
    """Parse data/<name> via `parser`, caching by mtime. Returns None if missing."""
    path = data_path(name)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None

    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        parsed = parser(path)
    except Exception as exc:  # noqa: BLE001 — a malformed file must not break calls
        log.warning("Failed to parse %s: %s", path, exc)
        return None

    _cache[name] = (mtime, parsed)
    return parsed
