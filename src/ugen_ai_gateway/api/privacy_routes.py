"""Privacy rules managed by administrators: custom sensitive terms, redacted
like built-in personal data (which is always redacted)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ugen_ai_gateway.analysis.custom_rules import PrivacySettings, rules_finder
from ugen_ai_gateway.analysis.pii import find_personal_data
from ugen_ai_gateway.analysis.redaction import RedactionVault
from ugen_ai_gateway.api.access import audit_admin, check_admin_access
from ugen_ai_gateway.dependencies import (
    PRIVACY_SETTING,
    current_privacy,
    get_gateway_service,
    get_usage_store,
    models_changed,
)
from ugen_ai_gateway.services.usage import UsageStore

router = APIRouter(prefix="/v1/ugen/privacy")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]


def _view(privacy: PrivacySettings) -> dict[str, Any]:
    return privacy.model_dump()


@router.get("")
async def get_privacy(store: UsageStoreDependency, request: Request) -> dict[str, Any]:
    await check_admin_access(request, store)
    return _view(current_privacy(store))


@router.put("")
async def save_privacy(
    body: PrivacySettings, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    await check_admin_access(request, store)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Saving needs a database; set USAGE_DB_PATH"
        )
    await store.save_setting(PRIVACY_SETTING, body.model_dump())
    models_changed()
    get_gateway_service()  # install the new rules now, not on the next chat
    await audit_admin(
        request,
        store,
        "privacy_updated",
        rules=[
            {"name": rule.name, "kind": rule.kind, "keep_local": rule.keep_local}
            for rule in body.rules
        ],
    )
    return _view(body)


class PrivacyTest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    # Rules being edited, tried without saving them.
    settings: PrivacySettings


@router.post("/test")
async def test_privacy(
    body: PrivacyTest, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Show what the cloud would receive for this text under these rules."""
    await check_admin_access(request, store)
    # The rules on screen are used for this preview only; requests running at
    # the same time keep using the saved ones.
    rules = body.settings.rules
    finder = rules_finder(rules) if rules else None
    matches = find_personal_data(body.text, custom_finder=finder)
    vault = RedactionVault(custom_finder=finder)
    vault.reserve([body.text])
    redacted = vault.redact(body.text)
    keep_local = set(body.settings.never_cloud_types())
    return {
        "redacted": redacted,
        "matches": [
            {"type": m.type, "label": m.label, "value": m.value} for m in matches
        ],
        "keeps_local": sorted({m.type for m in matches} & keep_local),
    }
