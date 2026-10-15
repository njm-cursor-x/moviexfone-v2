"""Frozen clock shared by the eval and the showtime tests."""

from __future__ import annotations

from datetime import datetime

from moviexfone.timeutil import NY

# Tuesday 13 October 2026, 3:00 PM Eastern.
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=NY)
