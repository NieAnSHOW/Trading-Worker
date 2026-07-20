"""Validated public and persisted models for the investment-news snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CANONICAL_TRACK_IDS = (
    "ai",
    "semi",
    "robot",
    "auto",
    "energy",
    "bio",
    "space",
    "security",
    "tech",
    "consumer",
    "macro",
    "science",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("datetime must be UTC aware")
    return value


class FeedSource(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)


class FeedItem(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    track_id: Literal[
        "ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"
    ]
    title: str = Field(min_length=1, max_length=300)
    title_zh: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=1000)
    source: FeedSource
    published_at: datetime | None = None
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("published_at")
    @classmethod
    def published_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value) if value is not None else value


class TrackAi(_StrictModel):
    available: bool = False
    generated_at: datetime | None = None
    highlights: list[str] = Field(default_factory=list, max_length=5)
    error: PublicError | None = None

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value) if value is not None else value

    @model_validator(mode="after")
    def validate_availability(self) -> "TrackAi":
        if self.available:
            if self.generated_at is None or not 3 <= len(self.highlights) <= 5 or self.error is not None:
                raise ValueError("available AI requires generation time and three to five highlights")
        elif self.generated_at is not None or self.highlights:
            raise ValueError("unavailable AI requires no generation or highlights")
        return self


class PublicError(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=200)


class TrackSnapshot(_StrictModel):
    track_id: Literal[
        "ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"
    ]
    state: Literal["fresh", "stale", "unavailable"]
    generated_at: datetime | None = None
    stale: bool = False
    items: list[FeedItem] = Field(default_factory=list, max_length=100)
    ai: TrackAi

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value) if value is not None else value

    @model_validator(mode="after")
    def validate_state(self) -> "TrackSnapshot":
        if any(item.track_id != self.track_id for item in self.items):
            raise ValueError("track items must match the track id")
        if self.state == "fresh" and self.generated_at is not None and not self.stale:
            return self
        if self.state == "stale" and self.generated_at is not None and self.stale:
            return self
        if self.state == "unavailable" and self.generated_at is None and not self.stale and not self.items:
            return self
        raise ValueError("track state does not match generated_at, stale, and items")


class NewsSnapshot(_StrictModel):
    schema_version: Literal[1]
    generated_at: datetime
    upstream_commit: str = Field(min_length=1, max_length=128)
    source_success_count: int = Field(ge=0)
    source_failure_count: int = Field(ge=0)
    errors: list[PublicError] = Field(default_factory=list, max_length=200)
    tracks: list[TrackSnapshot] = Field(min_length=len(CANONICAL_TRACK_IDS), max_length=len(CANONICAL_TRACK_IDS))

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime) -> datetime:
        return _validate_utc(value)

    @model_validator(mode="after")
    def validate_tracks(self) -> "NewsSnapshot":
        if tuple(track.track_id for track in self.tracks) != CANONICAL_TRACK_IDS:
            raise ValueError("tracks must be the canonical IDs in canonical order")
        return self


class RefreshStatus(_StrictModel):
    state: Literal["idle", "fetching", "normalizing", "enriching", "committing", "succeeded", "failed"]
    task_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    processed_endpoints: int = Field(default=0, ge=0)
    successful_endpoints: int = Field(default=0, ge=0)
    failed_endpoints: int = Field(default=0, ge=0)
    processed_tracks: int = Field(default=0, ge=0)
    total_endpoints: int = Field(default=0, ge=0)
    total_tracks: int = Field(default=len(CANONICAL_TRACK_IDS), ge=0)
    error: PublicError | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value) if value is not None else value


class SnapshotResponse(_StrictModel):
    available: bool
    stale: bool
    snapshot: NewsSnapshot | None
    refresh: RefreshStatus
    error: PublicError | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> "SnapshotResponse":
        if self.available != (self.snapshot is not None):
            raise ValueError("available must agree with snapshot")
        if not self.available and self.stale:
            raise ValueError("unavailable snapshot cannot be stale")
        return self


class RefreshAcceptedResponse(_StrictModel):
    task_id: UUID
    reused: bool
    status: RefreshStatus
