"""Authenticated, input-free HTTP routes for investment-news refreshes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status

from src.news.coordinator import NewsRefreshCoordinator
from src.news.models import RefreshAcceptedResponse, RefreshStatus, SnapshotResponse

logger = logging.getLogger(__name__)


class _NewsCoordinator(Protocol):
    async def snapshot_response(self) -> SnapshotResponse: ...

    async def start(self) -> RefreshAcceptedResponse: ...

    async def status(self) -> RefreshStatus: ...


async def _reject_input(request: Request) -> None:
    """Keep refresh input-free so callers cannot select feeds or LLM settings."""
    if request.query_params:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="News API accepts no parameters")
    if await request.body():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="News API accepts no request body")


def create_news_router(coordinator: NewsRefreshCoordinator) -> APIRouter:
    """Build routes backed by one already-created process coordinator."""
    router = APIRouter(prefix="/news-api", tags=["news"])

    @router.get("/snapshot", response_model=SnapshotResponse)
    async def get_snapshot(request: Request) -> SnapshotResponse:
        await _reject_input(request)
        return await coordinator.snapshot_response()

    @router.post("/refresh", response_model=RefreshAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def refresh_news(request: Request) -> RefreshAcceptedResponse:
        await _reject_input(request)
        try:
            return await coordinator.start()
        except RuntimeError:
            logger.warning("news refresh request rejected by unavailable coordinator")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="News refresh is unavailable") from None

    @router.get("/refresh/status", response_model=RefreshStatus)
    async def get_refresh_status(request: Request) -> RefreshStatus:
        await _reject_input(request)
        return await coordinator.status()

    return router


def register_news_routes(
    app: FastAPI,
    require_auth: Callable[..., Awaitable[None]],
    coordinator: NewsRefreshCoordinator,
) -> None:
    """Attach the news boundary with the server's existing auth dependency."""
    app.include_router(create_news_router(coordinator), dependencies=[Depends(require_auth)])
