"""System prompts added ahead of chat conversations.

The administrator's system prompt goes ahead of every request, from the
dashboard and the OpenAI-compatible API alike. Each account's custom
instructions go ahead of the requests it makes while signed in; API keys
never carry them, since an app writes its own prompts. Both are sent as system
messages, so they are checked and redacted like any other text before
reaching a cloud model. Account instructions are stored encrypted.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ugen_ai_gateway.api.access import (
    SESSION_PREFIX,
    audit_admin,
    check_admin_access,
    presented_api_key,
)
from ugen_ai_gateway.api.schemas import ChatRequest
from ugen_ai_gateway.dependencies import get_secret_box, get_usage_store
from ugen_ai_gateway.services.keys import Principal
from ugen_ai_gateway.services.usage import UsageStore

router = APIRouter(prefix="/v1/ugen")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]

SYSTEM_PROMPT_SETTING = "system_prompt"
MAX_SYSTEM_PROMPT_CHARACTERS = 8000
MAX_INSTRUCTIONS_CHARACTERS = 4000
INSTRUCTIONS_HEADING = "The user's custom instructions:"


class SystemPrompt(BaseModel):
    text: str = Field(default="", max_length=MAX_SYSTEM_PROMPT_CHARACTERS)


class Instructions(BaseModel):
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARACTERS)


def system_prompt(store: UsageStore | None) -> str:
    saved = store.load_setting(SYSTEM_PROMPT_SETTING) if store else None
    return str(saved.get("text") or "") if isinstance(saved, dict) else ""


async def with_prompts(
    request: ChatRequest, store: UsageStore | None, principal: Principal
) -> ChatRequest:
    """``request`` with the system prompt and the signed-in account's custom
    instructions ahead of its own messages."""
    prompts = [system_prompt(store)]
    if store is not None and principal.user_id:
        instructions = get_secret_box().open(
            await store.get_instructions(principal.user_id)
        )
        if instructions:
            prompts.append(f"{INSTRUCTIONS_HEADING}\n{instructions}")
    added = [{"role": "system", "content": text} for text in prompts if text]
    if not added:
        return request
    return request.model_copy(update={"messages": [*added, *request.messages]})


async def _session_user_id(
    request: Request, store: UsageStore | None
) -> tuple[UsageStore, str]:
    if store is None:
        raise HTTPException(
            status_code=503, detail="Accounts need a database; set USAGE_DB_PATH"
        )
    token = presented_api_key(request)
    user = await store.find_session(token) if token.startswith(SESSION_PREFIX) else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return store, user["id"]


@router.get("/auth/me/instructions")
async def get_instructions(
    store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    store, user_id = await _session_user_id(request, store)
    return {"text": get_secret_box().open(await store.get_instructions(user_id))}


@router.put("/auth/me/instructions")
async def save_instructions(
    body: Instructions, store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    """Set the custom instructions for the signed-in account; empty removes them."""
    store, user_id = await _session_user_id(request, store)
    text = body.text.strip()
    await store.set_instructions(user_id, get_secret_box().seal(text))
    return {"text": text}


@router.get("/system-prompt")
async def get_system_prompt(
    store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    await check_admin_access(request, store)
    return {"text": system_prompt(store)}


@router.put("/system-prompt")
async def save_system_prompt(
    body: SystemPrompt, store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    """Set the system prompt for every request; empty removes it."""
    await check_admin_access(request, store)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="The system prompt needs a database; set USAGE_DB_PATH",
        )
    text = body.text.strip()
    await store.save_setting(SYSTEM_PROMPT_SETTING, {"text": text})
    await audit_admin(request, store, "system_prompt_updated", characters=len(text))
    return {"text": text}
