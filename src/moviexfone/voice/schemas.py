"""Tool JSON schemas shared by the realtime session and the Builder bundle."""

from __future__ import annotations

from typing import Any

SEARCH_SHOWTIMES: dict[str, Any] = {
    "type": "function",
    "name": "search_showtimes",
    "description": (
        "Find movie showtimes at New York City theaters. Call ONCE per caller question. "
        "Never invent times; only read times returned here. Read the speak field aloud. "
        "If results are empty, read nearest_alternative or the speak field. "
        "Do not search again for the same question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Theater, neighborhood, landmark, subway stop, ZIP, or borough exactly as the caller said it.",
            },
            "movie": {
                "type": "string",
                "description": "Title as the caller said it. Omit for what's playing.",
            },
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD in America/New_York, or a phrase such as tonight, Friday, or this weekend. Default today.",
            },
            "after_time": {
                "type": "string",
                "description": "HH:MM 24-hour earliest start. Tonight means 17:00. Morning means 05:00.",
            },
            "before_time": {
                "type": "string",
                "description": "HH:MM 24-hour latest start. Morning means 12:00. Afternoon means 17:00.",
            },
            "format": {
                "type": "string",
                "enum": ["any", "imax", "dolby", "3d", "70mm", "35mm"],
                "default": "any",
            },
            "page": {
                "type": "integer",
                "default": 1,
                "description": "2, 3, ... only when the caller asks for more.",
            },
        },
        "required": ["location"],
    },
}

GET_MOVIE_INFO: dict[str, Any] = {
    "type": "function",
    "name": "get_movie_info",
    "description": "Rating, runtime, and a one-line synopsis for a movie playing in NYC.",
    "parameters": {
        "type": "object",
        "properties": {"movie": {"type": "string"}},
        "required": ["movie"],
    },
}

END_CALL: dict[str, Any] = {
    "type": "function",
    "name": "end_call",
    "description": "Hang up only after the caller says goodbye, bye, or thanks. Never call this because the caller is silent.",
    "parameters": {"type": "object", "properties": {}},
}


def tool_schemas() -> list[dict[str, Any]]:
    return [SEARCH_SHOWTIMES, GET_MOVIE_INFO, END_CALL]
