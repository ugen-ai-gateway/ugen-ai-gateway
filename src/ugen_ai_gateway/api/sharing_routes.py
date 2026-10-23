"""Sharing conversations: links others continue on their own, and shared
chats several accounts take part in.

A share link holds a snapshot of a conversation's messages and answers
(without its memory or routing details), encrypted like saved conversations.
It opens for people signed in to this gateway, or, if its owner chooses, for
anyone with the link; signed-in viewers continue it as their own copy, which
its owner never sees. A shared chat is one conversation that every member can
read and continue; see ``conversation_routes`` for how members' saves merge.
"""

import secrets
import zlib
from typing import Annotated, Any, Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ugen_ai_gateway.api.access import SESSION_PREFIX, presented_api_key, rate_limiter
from ugen_ai_gateway.api.conversation_routes import SavedConversation, decode, encode
from ugen_ai_gateway.dependencies import get_usage_store
from ugen_ai_gateway.services.usage import UsageStore

router = APIRouter(prefix="/v1/ugen")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]

# Opening share links, per client address per minute.
_SHARE_VIEWS_PER_MINUTE = 60
MAX_MEMBERS = 50


async def _user(request: Request, store: UsageStore | None) -> dict[str, Any] | None:
    if store is None:
        raise HTTPException(status_code=503, detail="Sharing needs a database")
    token = presented_api_key(request)
    return await store.find_session(token) if token.startswith(SESSION_PREFIX) else None


async def _signed_in(
    request: Request, store: UsageStore | None
) -> tuple[UsageStore, dict[str, Any]]:
    user = await _user(request, store)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to share conversations")
    assert store is not None
    return store, user


def snapshot(conversation: dict[str, Any]) -> dict[str, Any]:
    """What a share link shows: the title, and each question with its answer
    and the model that gave it. Memory, routing and usage details stay out."""
    turns = []
    for turn in conversation.get("turns", []):
        result = turn.get("result") or {}
        turns.append(
            {
                "id": turn.get("id"),
                "user": turn.get("user", ""),
                "author": turn.get("author"),
                "answer": result.get("content"),
                "model": result.get("model"),
                "provider": result.get("provider"),
                "stopped": bool(result.get("stopped")),
            }
        )
    return {"title": conversation.get("title") or "Conversation", "turns": turns}


class ShareRequest(BaseModel):
    conversation: SavedConversation
    visibility: Literal["accounts", "public"] = "accounts"


@router.post("/shares")
async def create_share(
    body: ShareRequest, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Share a conversation, or refresh its existing link with the current
    messages; the link stays the same."""
    store, user = await _signed_in(request, store)
    data = encode(snapshot(body.conversation.model_dump()))
    return await store.save_share(
        user["id"], body.conversation.id, body.visibility, data
    )


@router.get("/shares")
async def list_shares(store: UsageStoreDependency, request: Request) -> dict[str, Any]:
    store, user = await _signed_in(request, store)
    return {"data": await store.list_shares(user["id"])}


@router.delete("/shares/{share_id}", status_code=204)
async def delete_share(
    share_id: str, store: UsageStoreDependency, request: Request
) -> Response:
    store, user = await _signed_in(request, store)
    if not await store.delete_share(share_id, user["id"]):
        raise HTTPException(status_code=404, detail="Unknown share link")
    return Response(status_code=204)


@router.get("/shares/{share_id}")
async def view_share(
    share_id: str, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Open a share link. Links for accounts need a signed-in session; public
    links need nothing."""
    client = request.client.host if request.client else "unknown"
    if not rate_limiter.hit(
        f"share:{client}", limit=_SHARE_VIEWS_PER_MINUTE, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="Too many requests; wait a minute")
    viewer = await _user(request, store)
    assert store is not None
    share = await store.get_share(share_id)
    if share is None:
        raise HTTPException(
            status_code=404, detail="This link was removed or never existed"
        )
    if share["visibility"] != "public" and viewer is None:
        raise HTTPException(status_code=401, detail="Sign in to open this conversation")
    try:
        content = decode(share["data"])
    except (InvalidToken, zlib.error, ValueError) as exc:
        raise HTTPException(status_code=500, detail="This link cannot be read") from exc
    return content | {
        "owner": share["owner"],
        "visibility": share["visibility"],
        "updated_at": share["updated_at"],
        "is_owner": viewer is not None and viewer["id"] == share["owner_id"],
    }


class SharedChatRequest(BaseModel):
    conversation: SavedConversation
    members: list[str] = Field(min_length=1, max_length=MAX_MEMBERS)


class MemberChange(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)
    remove: list[str] = Field(default_factory=list, max_length=MAX_MEMBERS)


async def _member_ids(store: UsageStore, usernames: list[str]) -> list[str]:
    found = await store.find_usernames(usernames)
    missing = [name for name in usernames if name not in found]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"No active account named {', '.join(missing)}"
        )
    return list(found.values())


@router.post("/shared-chats", status_code=201)
async def create_shared_chat(
    body: SharedChatRequest, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Turn a conversation into a shared chat with these members. Returns the
    shared chat's id; the dashboard then replaces its own copy with it."""
    store, user = await _signed_in(request, store)
    member_ids = await _member_ids(store, body.members)
    chat_id = "shared-" + secrets.token_hex(8)
    conversation = body.conversation.model_dump() | {"id": chat_id}
    await store.create_shared_chat(
        chat_id, user["id"], encode(conversation), member_ids
    )
    return {"id": chat_id}


@router.patch("/shared-chats/{chat_id}")
async def change_members(
    chat_id: str, body: MemberChange, store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    """Add or remove members; only the owner may."""
    store, user = await _signed_in(request, store)
    add = await _member_ids(store, body.add) if body.add else []
    remove = await _member_ids(store, body.remove) if body.remove else []
    if not await store.change_shared_chat_members(chat_id, user["id"], add, remove):
        raise HTTPException(status_code=403, detail="Only the owner can change members")
    return {"status": "ok"}


@router.delete("/shared-chats/{chat_id}")
async def leave_shared_chat(
    chat_id: str, store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    """Leave a shared chat; for its owner this deletes it for everyone."""
    store, user = await _signed_in(request, store)
    outcome = await store.leave_shared_chat(chat_id, user["id"])
    if not outcome:
        raise HTTPException(status_code=404, detail="You are not in this shared chat")
    return {"status": outcome}
