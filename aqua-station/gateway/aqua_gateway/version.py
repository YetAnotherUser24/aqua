"""Which code is running: read once from the VERSION file the deploy script writes.

The gateway is installed on the station from a git checkout, not from a package index, so the
only reliable statement of "what ran" is the commit the deploy script copied. It writes
``/opt/aqua-station/VERSION`` (override with ``AQUA_VERSION_FILE``) as ``key=value`` lines;
events and export manifests carry it so every run is tied to a commit.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DEFAULT = "/opt/aqua-station/VERSION"


@lru_cache(maxsize=1)
def gateway_version() -> dict[str, str]:
    path = Path(os.environ.get("AQUA_VERSION_FILE", _DEFAULT))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"describe": "unknown", "commit": "unknown", "source": "no VERSION file"}
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out or {"describe": "unknown", "commit": "unknown", "source": "empty VERSION file"}


def recipe_digest(recipe: Any) -> str:
    """Short SHA-256 of a recipe's full definition (phases, conditions, commands, defaults)."""
    data = asdict(recipe) if is_dataclass(recipe) else recipe
    blob = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
