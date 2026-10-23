"""Administration from the server's command line.

    python -m ugen_ai_gateway.cli list-users
    python -m ugen_ai_gateway.cli reset-password <username> [--password <new>]
    python -m ugen_ai_gateway.cli make-admin <username>
    python -m ugen_ai_gateway.cli check-routing [--limit N]

With Docker: docker compose exec gateway python -m ugen_ai_gateway.cli ...
"""

import argparse
import asyncio
import json
import secrets
import sys
import time

from ugen_ai_gateway.config import get_settings
from ugen_ai_gateway.services.usage import UsageStore


def _store() -> UsageStore:
    path = get_settings().usage_db_path
    if not path:
        sys.exit("USAGE_DB_PATH is empty, so there are no accounts.")
    return UsageStore(path)


def _user(store: UsageStore, username: str) -> dict:
    user = store.get_user_sync(username)
    if user is None:
        sys.exit(f"No account named {username!r}.")
    return user


def _audit(store: UsageStore, event: str, user_id: str) -> None:
    store._append_audit(event, None, {"actor": "command_line", "user_id": user_id})


async def _check_routing(limit: int | None) -> None:
    """Run the labeled requests through this gateway's analysis, as configured
    (rules plus the analysis model), and report how often the local-or-cloud
    call matches the label."""
    from ugen_ai_gateway.analysis.complexity import CASES_FILE
    from ugen_ai_gateway.api.schemas import ChatRequest
    from ugen_ai_gateway.dependencies import get_gateway_service

    service = get_gateway_service()
    cases = json.loads(CASES_FILE.read_text("utf-8"))[:limit]
    sources: dict[str, int] = {}
    wrong: list[tuple[str, str, str, str]] = []
    gate = asyncio.Semaphore(4)

    async def check(case: dict) -> None:
        messages = []
        if case["previous"]:
            messages += [
                {"role": "user", "content": case["previous"]},
                {"role": "assistant", "content": "…"},
            ]
        messages.append({"role": "user", "content": case["text"]})
        request = ChatRequest(messages=messages)
        async with gate:
            analysis, _, source = await service._analyze(
                request,
                "\n".join(m["content"] for m in messages if m["role"] == "user"),
            )
        sources[source] = sources.get(source, 0) + 1
        if (analysis.complexity == "simple") != (case["label"] == "simple"):
            wrong.append((source, analysis.complexity, case["label"], case["text"]))
        done = sum(sources.values())
        if done % 25 == 0:
            print(f"  {done}/{len(cases)}", file=sys.stderr)

    await asyncio.gather(*(check(case) for case in cases))
    total = len(cases)
    print(f"{total - len(wrong)}/{total} right ({(total - len(wrong)) / total:.1%})")
    print("decided by: " + ", ".join(f"{k} {v}" for k, v in sorted(sources.items())))
    for source, got, label, text in wrong:
        print(f"  [{source}] got {got}, expected {label}: {text[:100]!r}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ugen_ai_gateway.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-users", help="list accounts")
    reset = commands.add_parser("reset-password", help="set a new password")
    reset.add_argument("username")
    reset.add_argument("--password", help="the new password (random when omitted)")
    admin = commands.add_parser("make-admin", help="make an account an administrator")
    admin.add_argument("username")
    check = commands.add_parser(
        "check-routing",
        help="measure how well requests are judged local or cloud, with this "
        "gateway's analysis model",
    )
    check.add_argument("--limit", type=int, help="only the first N requests")
    args = parser.parse_args(argv)

    if args.command == "check-routing":
        asyncio.run(_check_routing(args.limit))
        return

    store = _store()
    if args.command == "list-users":
        for user in store._list_users():
            role = "admin" if user["is_admin"] else "member"
            state = " (disabled)" if user["disabled"] else ""
            created = time.strftime("%Y-%m-%d", time.localtime(user["created_at"]))
            print(f"{user['username']:<32} {role:<7} created {created}{state}")
    elif args.command == "reset-password":
        user = _user(store, args.username)
        password = args.password or secrets.token_urlsafe(12)
        if len(password) < 8:
            sys.exit("Use a password of at least 8 characters.")
        store.set_password_sync(user["id"], password)
        store._update_user(user["id"], {"disabled": False})
        _audit(store, "password_reset", user["id"])
        print(f"New password for {user['username']}: {password}")
        print("Every session of this account was signed out.")
    elif args.command == "make-admin":
        user = _user(store, args.username)
        store._update_user(user["id"], {"is_admin": True, "disabled": False})
        _audit(store, "account_updated", user["id"])
        print(f"{user['username']} is now an administrator.")


if __name__ == "__main__":
    main()
