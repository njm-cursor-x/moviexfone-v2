"""Shared fixture clock: Tuesday 13 October 2026, 3:00 PM Eastern."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from moviexfone.catalog import seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.paths import data_dir
from moviexfone.timeutil import NY

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=NY)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    handle = Database(tmp_path / "test.db")
    seed(handle, data_dir(), now=NOW)
    yield handle
    handle.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        xai_api_key=None,
        live_voice=False,
        live_data=False,
        tool_secret="test-secret",
        db_path=tmp_path / "app.db",
        data_dir=data_dir(),
        bind="127.0.0.1",
        port=8000,
        cors_origins=("http://127.0.0.1:8000",),
        rate_limit_per_minute=60,
        assemble_ttl_seconds=900,
        xai_model="grok-voice-think-fast-2.0",
        max_session_seconds=900,
    )
