"""Authentication routes used by browser EventSource clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.security import SSETicketCapacityError, _mint_sse_ticket, require_auth


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/sse-ticket")
async def mint_sse_ticket(_: None = Depends(require_auth)) -> dict[str, str]:
    """Exchange header authentication for a one-time EventSource ticket."""
    try:
        return {"ticket": _mint_sse_ticket()}
    except SSETicketCapacityError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many active SSE streams") from exc
