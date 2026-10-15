"""Shared fixture clock: Tuesday 13 October 2026, 3:00 PM Eastern."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["MOVIEXFONE_TESTCLIENT"] = "1"
# The process default is a live fetch. Tests stay on fixtures and must not
# call Alamo, Nitehawk, or Assemble.
os.environ["MOVIEXFONE_LIVE_DATA"] = "0"

import pytest

from moviexfone.catalog import seed
from moviexfone.config import Settings
from moviexfone.db import Database
from moviexfone.paths import data_dir
from tests.clock import NOW

__all__ = ["NOW"]


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
