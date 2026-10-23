"""Request metadata, feedback, API keys, user accounts and saved
conversations, stored in SQLite.

Request records hold only metadata: routing, token counts, timings, cost, and
which personal-data types were found. Conversations that dashboard users save
are stored encrypted (see ``api/conversation_routes.py``); nothing else holds
message text or personal-data values.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    api_key_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    provider TEXT,
    provider_name TEXT,
    model TEXT,
    rule TEXT,
    task_type TEXT,
    complexity TEXT,
    analysis_source TEXT,
    personal_data_types TEXT NOT NULL DEFAULT '[]',
    redacted_values INTEGER NOT NULL DEFAULT 0,
    fallback_from TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    analysis_ms REAL NOT NULL DEFAULT 0,
    generation_ms REAL NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    baseline_cost_usd REAL NOT NULL DEFAULT 0,
    injection_signals TEXT NOT NULL DEFAULT '[]',
    output_redactions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS requests_created_at ON requests (created_at);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    request_id TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    revoked_at REAL,
    last_used_at REAL,
    allow_cloud INTEGER NOT NULL DEFAULT 1,
    monthly_budget_usd REAL,
    rate_limit_per_minute INTEGER
);
CREATE TABLE IF NOT EXISTS feedback (
    request_id TEXT PRIMARY KEY REFERENCES requests (id),
    rating INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login_at REAL,
    avatar TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    monthly_budget_usd REAL,
    oidc_subject TEXT,
    email TEXT,
    instructions TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions (user_id);
CREATE TABLE IF NOT EXISTS conversations (
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    -- Compressed, encrypted JSON of the whole conversation, title included.
    data BLOB NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS conversations_updated
    ON conversations (user_id, updated_at);
-- Read-only snapshots of a conversation, opened by link.
CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    visibility TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (owner_id, conversation_id)
);
-- Conversations several accounts take part in; data is encrypted like
-- saved conversations.
CREATE TABLE IF NOT EXISTS shared_chats (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    data BLOB NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_chat_members (
    chat_id TEXT NOT NULL REFERENCES shared_chats (id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS shared_chat_members_user
    ON shared_chat_members (user_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass
class UsageRecord:
    id: str
    status: str = "ok"
    error: str | None = None
    api_key_id: str | None = None
    user_id: str | None = None
    provider: str | None = None
    provider_name: str | None = None
    model: str | None = None
    rule: str | None = None
    task_type: str | None = None
    complexity: str | None = None
    analysis_source: str | None = None
    personal_data_types: list[str] = field(default_factory=list)
    redacted_values: int = 0
    fallback_from: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    analysis_ms: float = 0.0
    generation_ms: float = 0.0
    total_ms: float = 0.0
    cost_usd: float = 0.0
    baseline_cost_usd: float = 0.0
    injection_signals: list[str] = field(default_factory=list)
    output_redactions: int = 0
    created_at: float = field(default_factory=time.time)


def token_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_per_million: float,
    output_per_million: float,
) -> float:
    return (
        prompt_tokens * input_per_million + completion_tokens * output_per_million
    ) / 1_000_000


_ADDED_COLUMNS = {
    "requests": {
        "injection_signals": "TEXT NOT NULL DEFAULT '[]'",
        "output_redactions": "INTEGER NOT NULL DEFAULT 0",
        "user_id": "TEXT",
    },
    "users": {
        "avatar": "TEXT",
        "disabled": "INTEGER NOT NULL DEFAULT 0",
        "monthly_budget_usd": "REAL",
        "oidc_subject": "TEXT",
        "email": "TEXT",
        "instructions": "TEXT",
    },
}
_GENESIS_HASH = "0" * 64

# (api_key_id, user_id); None in either place means no restriction on it.
_Scope = tuple[str | None, str | None]
_SCOPE_SQL = "(? IS NULL OR api_key_id = ?) AND (? IS NULL OR user_id = ?)"


def _scope_args(scope: _Scope) -> tuple[str | None, ...]:
    api_key_id, user_id = scope
    return (api_key_id, api_key_id, user_id, user_id)


def _audit_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    canonical = json.dumps(
        entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


class UsageStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # Several gateway processes may share one file: wait for locks instead
        # of failing, and use WAL so readers do not block the writer.
        self._connection = sqlite3.connect(
            self._path, check_same_thread=False, timeout=30
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        if self._path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = Lock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in existing:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS requests_user_id ON requests (user_id)"
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_subject "
            "ON users (oidc_subject) WHERE oidc_subject IS NOT NULL"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # Writes and reads run in a worker thread so the event loop never waits on disk.

    async def record(self, record: UsageRecord) -> None:
        await asyncio.to_thread(self._record, record)

    # ``api_key_id`` and ``user_id`` scope reads and feedback to one tenant or
    # one user; None means no restriction.

    async def set_feedback(
        self,
        request_id: str,
        rating: int,
        *,
        api_key_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._set_feedback, request_id, rating, (api_key_id, user_id)
        )

    async def stats(
        self,
        *,
        days: int,
        tz_offset_minutes: int = 0,
        api_key_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._stats, days, tz_offset_minutes, (api_key_id, user_id)
        )

    async def recent(
        self,
        *,
        limit: int,
        before: float | None = None,
        api_key_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._recent, limit, before, (api_key_id, user_id)
        )

    def _record(self, record: UsageRecord) -> None:
        row = asdict(record)
        row["personal_data_types"] = json.dumps(record.personal_data_types)
        row["injection_signals"] = json.dumps(record.injection_signals)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT OR REPLACE INTO requests ({columns}) VALUES ({placeholders})",
                row,
            )

    def _set_feedback(self, request_id: str, rating: int, scope: _Scope) -> bool:
        with self._lock, self._connection:
            exists = self._connection.execute(
                f"SELECT 1 FROM requests WHERE id = ? AND {_SCOPE_SQL}",
                (request_id, *_scope_args(scope)),
            ).fetchone()
            if not exists:
                return False
            self._connection.execute(
                "INSERT INTO feedback (request_id, rating, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (request_id) DO UPDATE SET rating = excluded.rating, "
                "created_at = excluded.created_at",
                (request_id, rating, time.time()),
            )
            return True

    def _stats(
        self, days: int, tz_offset_minutes: int, scope_ids: _Scope
    ) -> dict[str, Any]:
        since = time.time() - days * 86400
        offset = tz_offset_minutes * 60
        scope = _SCOPE_SQL
        key = _scope_args(scope_ids)
        with self._lock:
            totals = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(status = 'ok'), 0) AS completed,
                    COALESCE(SUM(status = 'error'), 0) AS failed,
                    COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,
                    COALESCE(SUM(status = 'ok' AND provider = 'local'), 0) AS local,
                    COALESCE(SUM(status = 'ok' AND provider = 'cloud'), 0) AS cloud,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(generation_ms), 0) AS generation_ms,
                    COALESCE(SUM(cost_usd), 0) AS cost_usd,
                    COALESCE(SUM(baseline_cost_usd), 0) AS baseline_cost_usd,
                    COALESCE(SUM(personal_data_types != '[]'), 0) AS with_personal_data,
                    COALESCE(SUM(redacted_values), 0) AS redacted_values,
                    COALESCE(SUM(fallback_from IS NOT NULL), 0) AS fallbacks,
                    COALESCE(SUM(analysis_source = 'rules'), 0) AS analysis_rules,
                    COALESCE(SUM(analysis_source = 'cache'), 0) AS analysis_cache,
                    COALESCE(SUM(injection_signals != '[]'), 0) AS injection_flagged,
                    COALESCE(SUM(status = 'blocked'), 0) AS blocked,
                    COALESCE(SUM(output_redactions), 0) AS output_redactions,
                    COALESCE(AVG(CASE WHEN status = 'ok' THEN total_ms END), 0) AS avg_total_ms
                FROM requests WHERE created_at >= ? AND {scope}
                """.replace("{scope}", scope),
                (since, *key),
            ).fetchone()
            daily = self._connection.execute(
                """
                SELECT date(created_at + ?, 'unixepoch') AS day, COUNT(*) AS requests,
                       COALESCE(SUM(provider = 'cloud'), 0) AS cloud
                FROM requests WHERE created_at >= ? AND {scope}
                GROUP BY day ORDER BY day
                """.replace("{scope}", scope),
                (offset, since, *key),
            ).fetchall()
            rules = self._connection.execute(
                """
                SELECT COALESCE(rule, 'default') AS rule, COUNT(*) AS requests
                FROM requests WHERE created_at >= ? AND status = 'ok' AND {scope}
                GROUP BY rule ORDER BY requests DESC
                """.replace("{scope}", scope),
                (since, *key),
            ).fetchall()
            feedback = self._connection.execute(
                """
                SELECT COALESCE(SUM(rating = 1), 0) AS up, COALESCE(SUM(rating = -1), 0) AS down
                FROM feedback JOIN requests ON requests.id = feedback.request_id
                WHERE requests.created_at >= ? AND {scope}
                """.replace(
                    "{scope}",
                    scope.replace("api_key_id", "requests.api_key_id").replace(
                        "user_id", "requests.user_id"
                    ),
                ),
                (since, *key),
            ).fetchone()

        result = dict(totals)
        seconds = result.pop("generation_ms") / 1000
        result["tokens_per_second"] = (
            round(result["completion_tokens"] / seconds, 1) if seconds > 0 else None
        )
        result["savings_usd"] = max(
            result["baseline_cost_usd"] - result["cost_usd"], 0.0
        )
        result["days"] = days
        result["daily"] = [dict(row) for row in daily]
        result["rules"] = [dict(row) for row in rules]
        result["feedback"] = dict(feedback)
        return result

    def _recent(
        self, limit: int, before: float | None, scope: _Scope
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT requests.*, feedback.rating FROM requests "
            "LEFT JOIN feedback ON feedback.request_id = requests.id "
            "WHERE "
            + _SCOPE_SQL.replace("api_key_id", "requests.api_key_id").replace(
                "user_id", "requests.user_id"
            )
        )
        parameters: tuple[Any, ...] = _scope_args(scope)
        if before is not None:
            query += " AND requests.created_at < ?"
            parameters = (*parameters, before)
        query += " ORDER BY requests.created_at DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(query, (*parameters, limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["personal_data_types"] = json.loads(item["personal_data_types"])
            item["injection_signals"] = json.loads(item["injection_signals"])
            items.append(item)
        return items

    # Audit log: append-only, each entry hashes the previous entry's hash, so a
    # changed, removed or reordered row breaks verification from that point on.

    async def append_audit(
        self, event: str, *, request_id: str | None, detail: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(self._append_audit, event, request_id, detail)

    async def audit_entries(
        self, *, since: float | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._audit_entries, since, limit)

    async def verify_audit(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._verify_audit)

    def _append_audit(
        self, event: str, request_id: str | None, detail: dict[str, Any]
    ) -> None:
        # BEGIN IMMEDIATE takes SQLite's write lock before reading the last
        # hash, so processes sharing the file cannot both chain onto it.
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            last = self._connection.execute(
                "SELECT hash FROM audit ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["hash"] if last else _GENESIS_HASH
            entry = {
                "created_at": time.time(),
                "request_id": request_id,
                "event": event,
                "detail": detail,
            }
            self._connection.execute(
                "INSERT INTO audit (created_at, request_id, event, detail, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry["created_at"],
                    request_id,
                    event,
                    json.dumps(detail, sort_keys=True, ensure_ascii=False),
                    prev_hash,
                    _audit_hash(prev_hash, entry),
                ),
            )

    def _audit_entries(self, since: float | None, limit: int) -> list[dict[str, Any]]:
        # From ``since`` onwards, oldest first; without it, the newest entries
        # (still in order), so a capped export always includes recent events.
        with self._lock:
            if since is not None:
                rows = self._connection.execute(
                    "SELECT * FROM audit WHERE created_at >= ? ORDER BY seq LIMIT ?",
                    (since, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,)
                ).fetchall()[::-1]
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def _verify_audit(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit ORDER BY seq"
            ).fetchall()
        prev_hash = _GENESIS_HASH
        for row in rows:
            entry = {
                "created_at": row["created_at"],
                "request_id": row["request_id"],
                "event": row["event"],
                "detail": json.loads(row["detail"]),
            }
            if row["prev_hash"] != prev_hash or row["hash"] != _audit_hash(
                prev_hash, entry
            ):
                return {
                    "ok": False,
                    "entries": len(rows),
                    "first_invalid_seq": row["seq"],
                }
            prev_hash = row["hash"]
        return {"ok": True, "entries": len(rows), "first_invalid_seq": None}

    # API keys. Only a SHA-256 hash of each secret is stored; the secret itself
    # is returned once, when the key is created.

    async def create_key(
        self,
        *,
        name: str,
        allow_cloud: bool = True,
        monthly_budget_usd: float | None = None,
        rate_limit_per_minute: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        return await asyncio.to_thread(
            self._create_key,
            name,
            allow_cloud,
            monthly_budget_usd,
            rate_limit_per_minute,
        )

    async def list_keys(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_keys)

    async def revoke_key(self, key_id: str) -> bool:
        return await asyncio.to_thread(self._revoke_key, key_id)

    async def find_key(self, secret: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._find_key, secret)

    async def has_keys(self) -> bool:
        """Whether any API key was ever created, including revoked ones."""
        return await asyncio.to_thread(self._has_keys)

    async def month_spend(self, key_id: str) -> float:
        return await asyncio.to_thread(self._month_spend, key_id)

    def _create_key(
        self,
        name: str,
        allow_cloud: bool,
        monthly_budget_usd: float | None,
        rate_limit_per_minute: int | None,
    ) -> tuple[dict[str, Any], str]:
        secret = "ugk_" + secrets.token_urlsafe(32)
        row = {
            "id": "key_" + secrets.token_hex(6),
            "name": name,
            "prefix": secret[:10],
            "key_hash": _key_hash(secret),
            "created_at": time.time(),
            "allow_cloud": int(allow_cloud),
            "monthly_budget_usd": monthly_budget_usd,
            "rate_limit_per_minute": rate_limit_per_minute,
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO api_keys ({columns}) VALUES ({placeholders})", row
            )
        return self._key_view(row | {"revoked_at": None, "last_used_at": None}), secret

    def _list_keys(self) -> list[dict[str, Any]]:
        month_start = _month_start()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT api_keys.*, COALESCE(SUM(requests.cost_usd), 0) AS month_cost_usd,
                       COUNT(requests.id) AS month_requests
                FROM api_keys LEFT JOIN requests
                  ON requests.api_key_id = api_keys.id AND requests.created_at >= ?
                GROUP BY api_keys.id ORDER BY api_keys.created_at
                """,
                (month_start,),
            ).fetchall()
        return [self._key_view(dict(row)) for row in rows]

    def _revoke_key(self, key_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (time.time(), key_id),
            )
            return cursor.rowcount > 0

    def _find_key(self, secret: str) -> dict[str, Any] | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
                (_key_hash(secret),),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
        return self._key_view(dict(row))

    def _has_keys(self) -> bool:
        with self._lock:
            return (
                self._connection.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone()
                is not None
            )

    def _month_spend(self, key_id: str) -> float:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM requests "
                "WHERE api_key_id = ? AND created_at >= ?",
                (key_id, _month_start()),
            ).fetchone()
        return float(row["spend"])

    @staticmethod
    def _key_view(row: dict[str, Any]) -> dict[str, Any]:
        row.pop("key_hash", None)
        row["allow_cloud"] = bool(row["allow_cloud"])
        return row

    # Gateway settings changed from the dashboard, stored as JSON per key.

    def load_setting(self, key: str) -> Any | None:
        """Read a setting synchronously; it is small and read rarely."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    async def save_setting(self, key: str, value: Any) -> None:
        await asyncio.to_thread(self._save_setting, key, value)

    def _save_setting(self, key: str, value: Any) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), time.time()),
            )

    # User accounts. Passwords are stored as salted scrypt hashes and session
    # tokens as SHA-256 hashes; neither secret is ever written in plain text.

    async def has_users(self) -> bool:
        return await asyncio.to_thread(self._has_users)

    async def create_user(self, username: str, password: str) -> dict[str, Any]:
        """Create an account; the first account becomes the administrator.

        Raises ``UsernameTaken`` when the name is in use (case-insensitive).
        """
        password_hash = await asyncio.to_thread(hash_password, password)
        return await asyncio.to_thread(self._create_user, username, password_hash)

    async def verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._verify_login, username, password)

    async def create_session(
        self, user_id: str, *, ttl_seconds: float
    ) -> tuple[str, float]:
        """Return a new session token and its expiry time."""
        return await asyncio.to_thread(self._create_session, user_id, ttl_seconds)

    async def find_session(self, token: str) -> dict[str, Any] | None:
        """The user behind a live session token, or None."""
        return await asyncio.to_thread(self._find_session, token)

    async def delete_session(self, token: str) -> None:
        await asyncio.to_thread(self._delete_session, token)

    async def set_avatar(
        self, user_id: str, avatar: str | None
    ) -> dict[str, Any] | None:
        """Store (or with None, remove) a user's avatar image as a data URL."""
        return await asyncio.to_thread(self._set_avatar, user_id, avatar)

    async def get_instructions(self, user_id: str) -> str:
        """A user's custom instructions, as stored (sealed by the caller)."""
        return await asyncio.to_thread(self._get_instructions, user_id)

    async def set_instructions(self, user_id: str, instructions: str) -> None:
        await asyncio.to_thread(self._set_instructions, user_id, instructions)

    async def change_password(
        self, user_id: str, password: str, *, keep_session: str | None = None
    ) -> None:
        """Set a new password and end every other session of the account."""
        password_hash = await asyncio.to_thread(hash_password, password)
        await asyncio.to_thread(
            self._set_password, user_id, password_hash, keep_session
        )

    def set_password_sync(self, user_id: str, password: str) -> None:
        """For the command line: set a password and end every session."""
        self._set_password(user_id, hash_password(password), None)

    def _set_password(
        self, user_id: str, password_hash: str, keep_session: str | None
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            self._connection.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, _key_hash(keep_session) if keep_session else ""),
            )

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_user_sync, user_id)

    def get_user_sync(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ? OR username = ?", (user_id, user_id)
            ).fetchone()
        return _user_view(dict(row)) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        """Every account with this month's requests and cloud cost, most
        expensive first."""
        return await asyncio.to_thread(self._list_users)

    def _list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT users.*, COUNT(requests.id) AS month_requests,
                       COALESCE(SUM(requests.cost_usd), 0) AS month_cost_usd,
                       COALESCE(SUM(requests.provider = 'cloud'), 0) AS month_cloud
                FROM users LEFT JOIN requests
                  ON requests.user_id = users.id AND requests.created_at >= ?
                GROUP BY users.id
                ORDER BY month_cost_usd DESC, month_requests DESC, users.created_at
                """,
                (_month_start(),),
            ).fetchall()
        return [
            _user_view(dict(row))
            | {
                "month_requests": row["month_requests"],
                "month_cost_usd": row["month_cost_usd"],
                "month_cloud": row["month_cloud"],
            }
            for row in rows
        ]

    async def update_user(
        self, user_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Change is_admin, disabled or monthly_budget_usd.

        Raises ``LastAdministrator`` when the change would leave no active
        administrator, and ``KeyError`` for an unknown account.
        """
        return await asyncio.to_thread(self._update_user, user_id, changes)

    def _update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"is_admin", "disabled", "monthly_budget_usd"}
        updates = {k: v for k, v in changes.items() if k in allowed}
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if updates:
                assignments = ", ".join(f"{name} = :{name}" for name in updates)
                cursor = self._connection.execute(
                    f"UPDATE users SET {assignments} WHERE id = :id",
                    {**updates, "id": user_id},
                )
                if cursor.rowcount == 0:
                    raise KeyError(user_id)
                admins = self._connection.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0"
                ).fetchone()[0]
                if admins == 0:
                    raise LastAdministrator(user_id)
                if updates.get("disabled"):
                    self._connection.execute(
                        "DELETE FROM sessions WHERE user_id = ?", (user_id,)
                    )
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return _user_view(dict(row))

    async def user_month_spend(self, user_id: str) -> float:
        return await asyncio.to_thread(self._user_month_spend, user_id)

    def _user_month_spend(self, user_id: str) -> float:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM requests "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, _month_start()),
            ).fetchone()
        return float(row["spend"])

    async def sso_user(
        self, subject: str, email: str | None, name_hint: str, *, create: bool
    ) -> dict[str, Any] | None:
        """The account linked to a single sign-on identity, signing it in.

        With ``create``, a missing account is made with a free username based
        on ``name_hint`` and no password (the first account becomes the
        administrator). Returns None when there is no account and ``create``
        is false; raises ``AccountDisabled`` for a disabled account.
        """
        return await asyncio.to_thread(
            self._sso_user, subject, email, name_hint, create
        )

    def _sso_user(
        self, subject: str, email: str | None, name_hint: str, create: bool
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM users WHERE oidc_subject = ?", (subject,)
            ).fetchone()
            if row is not None:
                if row["disabled"]:
                    raise AccountDisabled(row["username"])
                self._connection.execute(
                    "UPDATE users SET last_login_at = ?, email = COALESCE(?, email) "
                    "WHERE id = ?",
                    (now, email, row["id"]),
                )
                return _user_view(dict(row) | {"last_login_at": now})
            if not create:
                return None
            first = (
                self._connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
                is None
            )
            username = name_hint
            for attempt in range(2, 1000):
                taken = self._connection.execute(
                    "SELECT 1 FROM users WHERE username = ?", (username,)
                ).fetchone()
                if taken is None:
                    break
                username = f"{name_hint}-{attempt}"
            row = {
                "id": "usr_" + secrets.token_hex(8),
                "username": username,
                "password_hash": NO_PASSWORD,
                "is_admin": int(first),
                "created_at": now,
                "last_login_at": now,
                "oidc_subject": subject,
                "email": email,
            }
            self._connection.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at, "
                "last_login_at, oidc_subject, email) VALUES (:id, :username, "
                ":password_hash, :is_admin, :created_at, :last_login_at, "
                ":oidc_subject, :email)",
                row,
            )
        return _user_view(row)

    # Saved conversations: opaque encrypted blobs, one per conversation.

    async def list_conversations(
        self, user_id: str, since: float | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Conversations changed after ``since`` (all when None), and the ids
        of every conversation the user has, so clients can drop deleted ones."""
        return await asyncio.to_thread(self._list_conversations, user_id, since)

    def _list_conversations(
        self, user_id: str, since: float | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, data, updated_at FROM conversations WHERE user_id = ? "
                "AND (? IS NULL OR updated_at > ?) ORDER BY updated_at DESC",
                (user_id, since, since),
            ).fetchall()
            ids = [
                row["id"]
                for row in self._connection.execute(
                    "SELECT id FROM conversations WHERE user_id = ? "
                    "ORDER BY updated_at DESC",
                    (user_id,),
                )
            ]
        return [dict(row) for row in rows], ids

    async def sync_conversations(
        self,
        user_id: str,
        save: list[tuple[str, bytes, int]],
        delete: list[str],
        *,
        max_total_bytes: int,
    ) -> float:
        """Store (id, data, size) items and delete ids in one transaction;
        returns the server time of the change. Raises ``StorageFull`` when the
        user's saved conversations would exceed ``max_total_bytes``."""
        return await asyncio.to_thread(
            self._sync_conversations, user_id, save, delete, max_total_bytes
        )

    def _sync_conversations(
        self,
        user_id: str,
        save: list[tuple[str, bytes, int]],
        delete: list[str],
        max_total_bytes: int,
    ) -> float:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.executemany(
                "DELETE FROM conversations WHERE user_id = ? AND id = ?",
                [(user_id, conversation_id) for conversation_id in delete],
            )
            self._connection.executemany(
                "INSERT INTO conversations "
                "(user_id, id, data, size, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, id) DO UPDATE SET "
                "data = excluded.data, size = excluded.size, "
                "updated_at = excluded.updated_at",
                [(user_id, cid, data, size, now, now) for cid, data, size in save],
            )
            total = self._connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM conversations WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if save and total > max_total_bytes:
                # Rolls the transaction back.
                raise StorageFull(total)
        return now

    # Share links: read-only snapshots, one per conversation and owner.

    async def save_share(
        self, owner_id: str, conversation_id: str, visibility: str, data: bytes
    ) -> dict[str, Any]:
        """Create or refresh the share of a conversation; its id stays the same."""
        return await asyncio.to_thread(
            self._save_share, owner_id, conversation_id, visibility, data
        )

    def _save_share(
        self, owner_id: str, conversation_id: str, visibility: str, data: bytes
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO shares (id, owner_id, conversation_id, visibility, data, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (owner_id, conversation_id) DO UPDATE SET "
                "visibility = excluded.visibility, data = excluded.data, "
                "updated_at = excluded.updated_at",
                (
                    "shr_" + secrets.token_urlsafe(12),
                    owner_id,
                    conversation_id,
                    visibility,
                    data,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT id, conversation_id, visibility, created_at, updated_at "
                "FROM shares WHERE owner_id = ? AND conversation_id = ?",
                (owner_id, conversation_id),
            ).fetchone()
        return dict(row)

    async def get_share(self, share_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_share, share_id)

    def _get_share(self, share_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT shares.*, users.username AS owner FROM shares "
                "JOIN users ON users.id = shares.owner_id "
                "WHERE shares.id = ? AND users.disabled = 0",
                (share_id,),
            ).fetchone()
        return dict(row) if row else None

    async def list_shares(self, owner_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_shares, owner_id)

    def _list_shares(self, owner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, conversation_id, visibility, created_at, updated_at "
                "FROM shares WHERE owner_id = ? ORDER BY updated_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def delete_share(self, share_id: str, owner_id: str) -> bool:
        return await asyncio.to_thread(self._delete_share, share_id, owner_id)

    def _delete_share(self, share_id: str, owner_id: str) -> bool:
        with self._lock, self._connection:
            return (
                self._connection.execute(
                    "DELETE FROM shares WHERE id = ? AND owner_id = ?",
                    (share_id, owner_id),
                ).rowcount
                > 0
            )

    # Shared chats: one encrypted conversation, readable and writable by its
    # members (the owner is a member too).

    async def find_usernames(self, usernames: list[str]) -> dict[str, str]:
        """Active accounts by username (case-insensitive): username -> id."""
        return await asyncio.to_thread(self._find_usernames, usernames)

    def _find_usernames(self, usernames: list[str]) -> dict[str, str]:
        found = {}
        with self._lock:
            for name in usernames:
                row = self._connection.execute(
                    "SELECT id, username FROM users WHERE username = ? AND disabled = 0",
                    (name,),
                ).fetchone()
                if row:
                    found[name] = row["id"]
        return found

    async def create_shared_chat(
        self, chat_id: str, owner_id: str, data: bytes, member_ids: list[str]
    ) -> None:
        await asyncio.to_thread(
            self._create_shared_chat, chat_id, owner_id, data, member_ids
        )

    def _create_shared_chat(
        self, chat_id: str, owner_id: str, data: bytes, member_ids: list[str]
    ) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO shared_chats (id, owner_id, data, size, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, owner_id, data, len(data), now, now),
            )
            self._connection.executemany(
                "INSERT OR IGNORE INTO shared_chat_members (chat_id, user_id, added_at) "
                "VALUES (?, ?, ?)",
                [(chat_id, uid, now) for uid in dict.fromkeys([owner_id, *member_ids])],
            )

    async def shared_chats_for(
        self, user_id: str, since: float | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Shared chats of a member changed after ``since`` (with owner and
        member names), and the ids of all of them."""
        return await asyncio.to_thread(self._shared_chats_for, user_id, since)

    def _shared_chats_for(
        self, user_id: str, since: float | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT chat.id, chat.owner_id, chat.data, chat.updated_at, "
                "owner.username AS owner FROM shared_chats AS chat "
                "JOIN shared_chat_members AS me ON me.chat_id = chat.id "
                "JOIN users AS owner ON owner.id = chat.owner_id "
                "WHERE me.user_id = ? AND (? IS NULL OR chat.updated_at > ?)",
                (user_id, since, since),
            ).fetchall()
            ids = [
                row["chat_id"]
                for row in self._connection.execute(
                    "SELECT chat_id FROM shared_chat_members WHERE user_id = ?",
                    (user_id,),
                )
            ]
            chats = []
            for row in rows:
                members = [
                    member["username"]
                    for member in self._connection.execute(
                        "SELECT users.username FROM shared_chat_members AS m "
                        "JOIN users ON users.id = m.user_id WHERE m.chat_id = ? "
                        "ORDER BY m.user_id != ?, m.added_at, users.username",
                        (row["id"], row["owner_id"]),
                    )
                ]
                chats.append(dict(row) | {"members": members})
        return chats, ids

    async def update_shared_chat(
        self, chat_id: str, user_id: str, merge: Any
    ) -> float | None:
        """Replace a shared chat's data with ``merge(current_data)`` in one
        transaction; None when the user is not a member."""
        return await asyncio.to_thread(
            self._update_shared_chat, chat_id, user_id, merge
        )

    def _update_shared_chat(
        self, chat_id: str, user_id: str, merge: Any
    ) -> float | None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT chat.data FROM shared_chats AS chat JOIN shared_chat_members "
                "AS m ON m.chat_id = chat.id WHERE chat.id = ? AND m.user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if row is None:
                return None
            data = merge(row["data"])
            self._connection.execute(
                "UPDATE shared_chats SET data = ?, size = ?, updated_at = ? WHERE id = ?",
                (data, len(data), now, chat_id),
            )
        return now

    async def is_shared_chat(self, chat_id: str) -> bool:
        return await asyncio.to_thread(self._is_shared_chat, chat_id)

    def _is_shared_chat(self, chat_id: str) -> bool:
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT 1 FROM shared_chats WHERE id = ?", (chat_id,)
                ).fetchone()
                is not None
            )

    async def change_shared_chat_members(
        self, chat_id: str, owner_id: str, add: list[str], remove: list[str]
    ) -> bool:
        """Add or remove members (by id); only the owner may, and the owner
        cannot be removed. False when the chat is not this owner's."""
        return await asyncio.to_thread(
            self._change_shared_chat_members, chat_id, owner_id, add, remove
        )

    def _change_shared_chat_members(
        self, chat_id: str, owner_id: str, add: list[str], remove: list[str]
    ) -> bool:
        now = time.time()
        with self._lock, self._connection:
            owned = self._connection.execute(
                "SELECT 1 FROM shared_chats WHERE id = ? AND owner_id = ?",
                (chat_id, owner_id),
            ).fetchone()
            if owned is None:
                return False
            self._connection.executemany(
                "INSERT OR IGNORE INTO shared_chat_members (chat_id, user_id, added_at) "
                "VALUES (?, ?, ?)",
                [(chat_id, uid, now) for uid in add],
            )
            self._connection.executemany(
                "DELETE FROM shared_chat_members WHERE chat_id = ? AND user_id = ?",
                [(chat_id, uid) for uid in remove if uid != owner_id],
            )
            # Members' next refresh picks up the new member list.
            self._connection.execute(
                "UPDATE shared_chats SET updated_at = ? WHERE id = ?", (now, chat_id)
            )
        return True

    async def leave_shared_chat(self, chat_id: str, user_id: str) -> str:
        """The owner leaving deletes the chat for everyone: returns "deleted",
        "left", or "" when the user was not a member."""
        return await asyncio.to_thread(self._leave_shared_chat, chat_id, user_id)

    def _leave_shared_chat(self, chat_id: str, user_id: str) -> str:
        with self._lock, self._connection:
            deleted = self._connection.execute(
                "DELETE FROM shared_chats WHERE id = ? AND owner_id = ?",
                (chat_id, user_id),
            ).rowcount
            if deleted:
                return "deleted"
            left = self._connection.execute(
                "DELETE FROM shared_chat_members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).rowcount
            if left:
                self._connection.execute(
                    "UPDATE shared_chats SET updated_at = ? WHERE id = ?",
                    (time.time(), chat_id),
                )
        return "left" if left else ""

    async def link_sso(
        self, user_id: str, subject: str, email: str | None
    ) -> dict[str, Any]:
        """Link a single sign-on identity to an existing account. Raises
        ``IdentityTaken`` (with the other account's username) when another
        account already uses it, and ``KeyError`` for an unknown account."""
        return await asyncio.to_thread(self._link_sso, user_id, subject, email)

    def _link_sso(
        self, user_id: str, subject: str, email: str | None
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            other = self._connection.execute(
                "SELECT username FROM users WHERE oidc_subject = ? AND id != ?",
                (subject, user_id),
            ).fetchone()
            if other is not None:
                raise IdentityTaken(other["username"])
            updated = self._connection.execute(
                "UPDATE users SET oidc_subject = ?, email = COALESCE(?, email) "
                "WHERE id = ?",
                (subject, email, user_id),
            ).rowcount
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not updated or row is None:
            raise KeyError(user_id)
        return _user_view(dict(row))

    async def unlink_sso(self, user_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._unlink_sso, user_id)

    def _unlink_sso(self, user_id: str) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET oidc_subject = NULL WHERE id = ?", (user_id,)
            )
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return _user_view(dict(row))

    async def delete_user(self, user_id: str) -> bool:
        """Delete an account with its sessions, request metadata and feedback.

        Audit entries stay: they never name the user, and removing them would
        break the hash chain. When the last administrator leaves, the oldest
        remaining account is promoted so the gateway stays manageable.
        """
        return await asyncio.to_thread(self._delete_user, user_id)

    def _has_users(self) -> bool:
        with self._lock:
            return (
                self._connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
                is not None
            )

    def _create_user(self, username: str, password_hash: str) -> dict[str, Any]:
        with self._lock, self._connection:
            # Take the write lock first so two simultaneous sign-ups cannot
            # both become the first (administrator) account.
            self._connection.execute("BEGIN IMMEDIATE")
            first = (
                self._connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
                is None
            )
            row = {
                "id": "usr_" + secrets.token_hex(8),
                "username": username,
                "password_hash": password_hash,
                "is_admin": int(first),
                "created_at": time.time(),
            }
            try:
                self._connection.execute(
                    "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
                    "VALUES (:id, :username, :password_hash, :is_admin, :created_at)",
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise UsernameTaken(username) from exc
        return _user_view(row | {"last_login_at": None})

    def _verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            # Spend the same time as a real check so timing does not reveal
            # which usernames exist.
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        if row["disabled"]:
            raise AccountDisabled(row["username"])
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"])
            )
        return _user_view(dict(row) | {"last_login_at": now})

    def _create_session(self, user_id: str, ttl_seconds: float) -> tuple[str, float]:
        token = "ugs_" + secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + ttl_seconds
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            )
            self._connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_key_hash(token), user_id, now, expires_at),
            )
        return token, expires_at

    def _find_session(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ? "
                "AND users.disabled = 0",
                (_key_hash(token), time.time()),
            ).fetchone()
        return _user_view(dict(row)) if row else None

    def _delete_session(self, token: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_key_hash(token),)
            )

    def _get_instructions(self, user_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT instructions FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return (row["instructions"] or "") if row else ""

    def _set_instructions(self, user_id: str, instructions: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET instructions = ? WHERE id = ?",
                (instructions or None, user_id),
            )

    def _set_avatar(self, user_id: str, avatar: str | None) -> dict[str, Any] | None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET avatar = ? WHERE id = ?", (avatar, user_id)
            )
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_view(dict(row)) if row else None

    def _delete_user(self, user_id: str) -> bool:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "DELETE FROM feedback WHERE request_id IN "
                "(SELECT id FROM requests WHERE user_id = ?)",
                (user_id,),
            )
            self._connection.execute(
                "DELETE FROM requests WHERE user_id = ?", (user_id,)
            )
            self._connection.execute(
                "DELETE FROM sessions WHERE user_id = ?", (user_id,)
            )
            self._connection.execute(
                "DELETE FROM conversations WHERE user_id = ?", (user_id,)
            )
            deleted = self._connection.execute(
                "DELETE FROM users WHERE id = ?", (user_id,)
            ).rowcount
            if deleted:
                self._connection.execute(
                    "UPDATE users SET is_admin = 1 WHERE id = "
                    "(SELECT id FROM users WHERE disabled = 0 ORDER BY created_at LIMIT 1) "
                    "AND NOT EXISTS (SELECT 1 FROM users WHERE is_admin = 1 AND disabled = 0)"
                )
            return deleted > 0


def _key_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _month_start() -> float:
    """Start of the current calendar month, UTC."""
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC).timestamp()


# Stored for accounts created by single sign-on; no password matches it.
NO_PASSWORD = "!"


class IdentityTaken(ValueError):
    """Another account already uses this single sign-on identity."""


class StorageFull(ValueError):
    """Saving would exceed the per-account conversation storage limit."""


class UsernameTaken(ValueError):
    pass


class AccountDisabled(PermissionError):
    pass


class LastAdministrator(ValueError):
    """The change would leave the gateway without an active administrator."""


def _user_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        "last_login_at": row.get("last_login_at"),
        "avatar": row.get("avatar"),
        "disabled": bool(row.get("disabled") or 0),
        "monthly_budget_usd": row.get("monthly_budget_usd"),
        "email": row.get("email"),
        "sso": bool(row.get("oidc_subject")),
        "has_password": row.get("password_hash", NO_PASSWORD) != NO_PASSWORD,
    }


# scrypt parameters (RFC 7914 interactive settings): about 16 MB and tens of
# milliseconds per hash. They are stored with each hash so they can change.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    encode = base64.b64encode
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{encode(salt).decode()}${encode(digest).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
