from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    """Who is calling, and what their API key allows.

    ``api_key_id`` is None for the master GATEWAY_API_KEY, for signed-in
    users and for open (keyless) development mode. ``user_id`` is set for a
    signed-in dashboard user; members see only their own usage, while
    administrators (and the master key) see everything.
    """

    api_key_id: str | None
    allow_cloud: bool = True
    monthly_budget_usd: float | None = None
    rate_limit_per_minute: int | None = None
    user_id: str | None = None
    username: str | None = None
    is_admin: bool = False

    @property
    def usage_scope(self) -> dict[str, str | None]:
        """Filters for usage reads: an API key or a member sees only its own."""
        return {
            "api_key_id": self.api_key_id,
            "user_id": None if self.is_admin else self.user_id,
        }

    @classmethod
    def from_key(cls, key: dict[str, Any]) -> "Principal":
        return cls(
            api_key_id=key["id"],
            allow_cloud=bool(key["allow_cloud"]),
            monthly_budget_usd=key["monthly_budget_usd"],
            rate_limit_per_minute=key["rate_limit_per_minute"],
        )

    @classmethod
    def from_user(cls, user: dict[str, Any]) -> "Principal":
        return cls(
            api_key_id=None,
            user_id=user["id"],
            username=user["username"],
            is_admin=bool(user["is_admin"]),
            monthly_budget_usd=user.get("monthly_budget_usd"),
        )
