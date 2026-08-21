"""Anonymous, owner-scoped chat history endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.session import SessionAccessError, SessionNotFoundError, sessions

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
ClientId = Annotated[str, Query(min_length=16, max_length=64)]


class CreateSessionRequest(BaseModel):
    client_id: str = Field(min_length=16, max_length=64)


class UpdateSessionRequest(BaseModel):
    client_id: str = Field(min_length=16, max_length=64)
    title: str = Field(min_length=1, max_length=80)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="گفتگو پیدا نشد.")


@router.get("")
async def list_sessions(client_id: ClientId) -> dict[str, Any]:
    return {"items": sessions.list(client_id)}


@router.post("", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    session = sessions.create(req.client_id)
    return {
        "id": session.id, "title": session.title,
        "created_at": session.created_at, "updated_at": session.touched,
        "message_count": 0,
    }


@router.delete("/empty")
async def delete_empty_sessions(client_id: ClientId) -> dict[str, int]:
    """Cleanup empty chats left behind by refreshes or closed tabs."""
    return {"deleted": sessions.delete_empty(client_id)}


@router.get("/{session_id}")
async def get_session(session_id: str, client_id: ClientId) -> dict[str, Any]:
    try:
        session, messages = sessions.messages(session_id, client_id)
    except (SessionNotFoundError, SessionAccessError) as exc:
        raise _not_found() from exc
    return {
        "id": session.id, "title": session.title,
        "created_at": session.created_at, "updated_at": session.touched,
        "messages": messages,
    }


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    req: Annotated[UpdateSessionRequest, Body()],
) -> dict[str, str]:
    try:
        sessions.rename(session_id, req.client_id, req.title)
    except (SessionNotFoundError, SessionAccessError) as exc:
        raise _not_found() from exc
    return {"id": session_id, "title": req.title.strip()}


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, client_id: ClientId) -> Response:
    try:
        sessions.delete(session_id, client_id)
    except (SessionNotFoundError, SessionAccessError) as exc:
        raise _not_found() from exc
    return Response(status_code=204)
