"""Authenticated HTTP route for global persisted LLM usage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query

from src.usage.llm_aggregation import LLMUsageAggregationService
from src.usage.models import LLMUsageAggregateResponse


AuthDep = Callable[..., Awaitable[Any] | Any]
_MAX_TIMEZONE_NAME_LENGTH = 255


def _parse_iso8601(value: str | None, name: str) -> datetime | None:
    """Parse an offset-bearing ISO-8601 boundary into UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise HTTPException(status_code=400, detail=f"{name} must include a timezone offset")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid {name}") from exc


def _validate_timezone(timezone_name: str) -> None:
    """Reject unknown IANA timezone names before aggregation work begins."""
    if len(timezone_name) > _MAX_TIMEZONE_NAME_LENGTH:
        raise HTTPException(status_code=400, detail="invalid timezone")
    try:
        ZoneInfo(timezone_name)
    except (OSError, ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=400, detail="invalid timezone") from exc


def _parse_page_value(value: str, name: str, *, maximum: int | None = None) -> int:
    """Parse positive pagination input while keeping malformed values as 400s."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid {name}") from exc
    if parsed < 1 or (maximum is not None and parsed > maximum):
        detail = f"{name} must be at least 1" if maximum is None else f"{name} must be between 1 and {maximum}"
        raise HTTPException(status_code=400, detail=detail)
    return parsed


def register_llm_usage_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the global LLM usage endpoint using host-owned auth and state."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_llm_usage_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )
    if require_auth is None:
        require_auth = host.require_auth

    def _host_get_session_service():
        current_host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return current_host._get_session_service()

    @app.get(
        "/usage/llm",
        response_model=LLMUsageAggregateResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_llm_usage(
        start_at: str | None = Query(None),
        end_at: str | None = Query(None),
        timezone: str = Query("UTC"),
        query: str | None = Query(None),
        page: str = Query("1"),
        page_size: str = Query("25"),
    ) -> LLMUsageAggregateResponse:
        """Return time-range totals plus independently paginated session rows."""
        if len(timezone) == 0:
            raise HTTPException(status_code=400, detail="invalid timezone")
        if query is not None and len(query) > 500:
            raise HTTPException(status_code=400, detail="query must not exceed 500 characters")
        parsed_page = _parse_page_value(page, "page")
        parsed_page_size = _parse_page_value(page_size, "page_size", maximum=100)
        parsed_start = _parse_iso8601(start_at, "start_at")
        parsed_end = _parse_iso8601(end_at, "end_at")
        if parsed_start is not None and parsed_end is not None and parsed_start >= parsed_end:
            raise HTTPException(status_code=400, detail="start_at must be before end_at")
        _validate_timezone(timezone)
        service = _host_get_session_service()
        if service is None:
            raise HTTPException(status_code=501, detail="Session runtime not enabled")
        aggregation_service = LLMUsageAggregationService(service.store, service.runs_dir)
        return await asyncio.to_thread(
            aggregation_service.aggregate,
            start_at=parsed_start,
            end_at=parsed_end,
            timezone_name=timezone,
            query=query,
            page=parsed_page,
            page_size=parsed_page_size,
        )
