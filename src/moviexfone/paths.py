"""Locate bundled data files in a checkout or a container."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("MOVIEXFONE_DATA")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / "data")
    candidates.append(Path(__file__).resolve().parents[2] / "data")
    for path in candidates:
        if (path / "nyc-theaters.csv").is_file():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"nyc-theaters.csv not found. Looked in: {searched}")
