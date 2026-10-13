"""Request models shared by the HTTP tools and the realtime tool loop."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

_FORMATS = {"any", "imax", "dolby", "3d", "70mm", "35mm"}


class SearchIn(BaseModel):
    location: str | None = Field(default=None, max_length=120)
    movie: str | None = Field(default=None, max_length=160)
    date: str | None = Field(default=None, max_length=40)
    after_time: str | None = Field(default=None, max_length=8)
    format: str = "any"
    page: int = Field(default=1, ge=1, le=10)

    @field_validator("location", "movie", "date", "after_time", mode="before")
    @classmethod
    def no_nulls(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        if "\x00" in value:
            raise ValueError("invalid character")
        text = value.strip()
        return text or None

    @field_validator("format", mode="before")
    @classmethod
    def known_format(cls, value: object) -> str:
        if value is None or value == "":
            return "any"
        text = str(value).strip().lower()
        if text not in _FORMATS:
            raise ValueError("invalid format")
        return text

    @field_validator("page", mode="before")
    @classmethod
    def page_or_one(cls, value: object) -> int:
        if value is None or value == "":
            return 1
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ValueError("invalid page")
        return int(value)


class MovieIn(BaseModel):
    movie: str = Field(min_length=1, max_length=160)

    @field_validator("movie")
    @classmethod
    def no_nulls(cls, value: str) -> str:
        if value is None or not isinstance(value, str):
            raise ValueError("invalid movie")
        if "\x00" in value:
            raise ValueError("invalid character")
        text = value.strip()
        if not text:
            raise ValueError("invalid movie")
        return text


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    page: int = Field(default=1, ge=1, le=10)
