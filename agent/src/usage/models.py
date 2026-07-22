"""Strict response models for global LLM usage aggregation."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_serializer

from src.api.models import LLMUsageTotals


class _AggregateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_serializer(mode="wrap")
    def _serialize_without_optional_nulls(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class LLMUsageAggregateTotals(_AggregateModel):
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    total_tokens: NonNegativeInt = 0
    calls: NonNegativeInt = 0
    runs: NonNegativeInt = 0
    sessions: NonNegativeInt = 0
    missing_usage_runs: NonNegativeInt = 0
    invalid_usage_runs: NonNegativeInt = 0
    cache_read_tokens: Optional[NonNegativeInt] = None
    cache_write_tokens: Optional[NonNegativeInt] = None
    cache_read_reported_runs: NonNegativeInt = 0
    cache_write_reported_runs: NonNegativeInt = 0


AggregateTotals = LLMUsageAggregateTotals


class RunRow(_AggregateModel):
    run_id: str = Field(min_length=1)
    occurred_at: datetime
    provider: str = Field(min_length=1)
    model: Optional[str] = None
    metering_eligible: Optional[bool] = None
    totals: LLMUsageTotals


class DailyBucket(_AggregateModel):
    date: date
    totals: LLMUsageTotals


class ModelBucket(_AggregateModel):
    provider: str = Field(min_length=1)
    model: Optional[str] = None
    totals: LLMUsageTotals


class SessionRow(_AggregateModel):
    session_id: str = Field(min_length=1)
    title: str
    last_run_at: datetime
    totals: LLMUsageTotals
    runs: list[RunRow]


class SessionPage(_AggregateModel):
    items: list[SessionRow]
    page: PositiveInt
    page_size: PositiveInt
    total_items: NonNegativeInt
    total_pages: NonNegativeInt


class Period(_AggregateModel):
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    @model_serializer(mode="wrap")
    def _serialize_period_boundaries(self, handler):
        """Keep both explicit range boundaries, including unset null values."""
        return handler(self)


class LLMUsageAggregateResponse(_AggregateModel):
    generated_at: datetime
    timezone: str = Field(min_length=1)
    period: Period
    totals: LLMUsageAggregateTotals
    trend: list[DailyBucket]
    breakdown: list[ModelBucket]
    sessions: SessionPage

    @property
    def models(self) -> list[ModelBucket]:
        """Read-only breakdown accessor for in-process callers; never serialized."""
        return self.breakdown
