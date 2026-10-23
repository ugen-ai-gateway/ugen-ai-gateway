"""Conversations saved for dashboard accounts, so history follows a person
across browsers and devices, and shared chats several accounts take part in.

Each conversation is stored as one compressed blob, encrypted with the
gateway's secret key (``secret.key`` or ``SECRET_KEY``); titles, messages and
any personal data in them are never written in plain text. Only the account
that saved a conversation can read it, administrators included.
"""

import json
import logging
import time
import zlib
from typing import Annotated, Any

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ugen_ai_gateway.api.access import SESSION_PREFIX, presented_api_key
from ugen_ai_gateway.config import get_settings
from ugen_ai_gateway.dependencies import get_secret_box, get_usage_store
from ugen_ai_gateway.services.usage import StorageFull, UsageStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/ugen/conversations")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]

# Uncompressed JSON size of one conversation.
MAX_CONVERSATION_BYTES = 5_000_000
CONVERSATION_ID = r"^[A-Za-z0-9_-]{1,64}$"


class SavedConversation(BaseModel):
    """The dashboard's conversation object; fields beyond ``id`` are kept as
    sent, so the dashboard can add fields without a server change."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=CONVERSATION_ID)


class SyncRequest(BaseModel):
    save: list[SavedConversation] = Field(default_factory=list, max_length=500)
    delete: list[Annotated[str, Field(pattern=CONVERSATION_ID)]] = Field(
        default_factory=list, max_length=5000
    )
    # Shared chats: turn ids removed on this device (deleted, regenerated or
    # edited), so the merge on the server drops them too.
    removed_turns: dict[str, list[str]] = Field(default_factory=dict)


def encode(conversation: dict[str, Any]) -> bytes:
    """Compress and encrypt a conversation for storage."""
    # Shared-chat details are added when listing, never stored.
    stored = {key: value for key, value in conversation.items() if key != "shared"}
    raw = json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_CONVERSATION_BYTES:
        raise HTTPException(
            status_code=413,
            detail="This conversation is too long to save; start a new one",
        )
    return get_secret_box().encrypt(zlib.compress(raw, 6))


def decode(data: bytes) -> dict[str, Any]:
    """Raises InvalidToken, zlib.error or ValueError when unreadable."""
    return json.loads(zlib.decompress(get_secret_box().decrypt(data)))


def merge_shared(
    stored: dict[str, Any], incoming: dict[str, Any], removed: set[str]
) -> dict[str, Any]:
    """Merge one member's copy of a shared chat into the stored one, turn by
    turn, so members writing at the same time do not overwrite each other.
    New turns are appended; a turn both have keeps the more complete copy
    (one with an answer beats one still waiting); removed turns are dropped.
    The title and memory come from the incoming copy."""
    turns = [turn for turn in stored.get("turns", []) if turn.get("id") not in removed]
    position = {turn.get("id"): index for index, turn in enumerate(turns)}
    for turn in incoming.get("turns", []):
        turn_id = turn.get("id")
        if turn_id in removed:
            continue
        if turn_id not in position:
            position[turn_id] = len(turns)
            turns.append(turn)
        elif turn.get("result") or not turns[position[turn_id]].get("result"):
            turns[position[turn_id]] = turn
    return {**stored, **incoming, "turns": turns}


async def _account(
    request: Request, store: UsageStore | None
) -> tuple[UsageStore, str]:
    if store is None:
        raise HTTPException(
            status_code=503, detail="Saving conversations needs a database"
        )
    token = presented_api_key(request)
    user = await store.find_session(token) if token.startswith(SESSION_PREFIX) else None
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to sync conversations")
    return store, user["id"]


@router.get("")
async def list_conversations(
    store: UsageStoreDependency, request: Request, since: float | None = None
) -> dict[str, Any]:
    """Conversations and shared chats changed after ``since`` (a
    ``server_time`` from an earlier response; omit it for all), and the ids of
    all of them. Shared chats carry a ``shared`` object (owner, members, the
    caller's role)."""
    store, user_id = await _account(request, store)
    # Taken before reading, so a save racing this request shows up next time.
    server_time = time.time()
    rows, ids = await store.list_conversations(user_id, since)
    chats, chat_ids = await store.shared_chats_for(user_id, since)
    conversations, unreadable = [], 0
    for row in rows:
        try:
            conversations.append(decode(row["data"]))
        except (InvalidToken, zlib.error, ValueError):
            unreadable += 1
    for chat in chats:
        try:
            conversation = decode(chat["data"])
        except (InvalidToken, zlib.error, ValueError):
            unreadable += 1
            continue
        conversation["id"] = chat["id"]
        conversation["shared"] = {
            "owner": chat["owner"],
            "members": chat["members"],
            "role": "owner" if chat["owner_id"] == user_id else "member",
        }
        conversations.append(conversation)
    if unreadable:
        logger.warning(
            "%d saved conversation(s) cannot be decrypted; was secret.key or "
            "SECRET_KEY changed?",
            unreadable,
        )
    return {
        "conversations": conversations,
        "ids": [*ids, *chat_ids],
        "server_time": server_time,
        "unreadable": unreadable,
    }


@router.post("/sync")
async def sync_conversations(
    body: SyncRequest, store: UsageStoreDependency, request: Request
) -> dict[str, float]:
    """Save and delete conversations in one step. A saved conversation
    replaces the stored one with the same id; a shared chat is merged turn by
    turn instead, and deleting it leaves it (or, for its owner, deletes it)."""
    store, user_id = await _account(request, store)
    items = []
    server_time = time.time()
    for conversation in body.save:
        if await store.is_shared_chat(conversation.id):
            incoming = conversation.model_dump()
            removed = set(body.removed_turns.get(conversation.id, []))

            def merge(data: bytes, incoming=incoming, removed=removed) -> bytes:
                return encode(merge_shared(decode(data), incoming, removed))

            if await store.update_shared_chat(conversation.id, user_id, merge) is None:
                raise HTTPException(
                    status_code=403, detail="You are no longer in this shared chat"
                )
            continue
        data = encode(conversation.model_dump())
        items.append((conversation.id, data, len(data)))
    personal_deletes = []
    for conversation_id in body.delete:
        if await store.is_shared_chat(conversation_id):
            await store.leave_shared_chat(conversation_id, user_id)
        else:
            personal_deletes.append(conversation_id)
    limit = int(get_settings().conversation_storage_mb * 1_000_000)
    try:
        server_time = await store.sync_conversations(
            user_id, items, personal_deletes, max_total_bytes=limit
        )
    except StorageFull as exc:
        raise HTTPException(
            status_code=507,
            detail="Your saved conversations are full; delete some old ones",
        ) from exc
    return {"server_time": server_time}
