# UGen AI Gateway

[![CI](https://github.com/baihufox3210/ugen-ai-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/baihufox3210/ugen-ai-gateway/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.143%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](#deployment)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

UGen AI Gateway sits between your apps and your language models. Every request
is analyzed on your own machine first: personal data is detected and replaced
with placeholders, and the request is routed to a local model or a cloud model
depending on its complexity and your privacy rules. Replies come back with the
original values restored, so users see a complete answer while the cloud model
never saw the data.

- **Privacy first**: reversible redaction of emails, phone numbers, national
  IDs, cards, keys and more, plus your own terms (project code names, client
  names), always on, with nothing to configure.
- **Smart routing**: simple requests stay on the local model, complex ones go
  to the cloud; extra models and routing rules set up on the dashboard;
  automatic fallback when a provider is down.
- **OpenAI-compatible API**: point any OpenAI SDK or tool at `/v1`, including
  local `/v1/embeddings`.
- **Dashboard**: streaming chat with long-term conversation memory, stop,
  edit and regenerate, search and export; usage and cost savings; dark mode.
- **Teams**: accounts with roles and monthly budgets, single sign-on (OpenID
  Connect), per-app API keys with budgets and rate limits, and a
  tamper-evident audit log.

## Quick start

You need [Docker](https://docs.docker.com/engine/install/) with the Compose
plugin, and an OpenAI-compatible model server for the local model, such as
[Ollama](https://ollama.com):

```sh
ollama pull qwen2.5:7b

git clone https://github.com/baihufox3210/ugen-ai-gateway.git
cd ugen-ai-gateway
docker compose up -d --build
```

Open `http://<host>:7632` and create the administrator account. The dashboard
then points you to the **Models** page (also under the gear icon in the
sidebar): enter your local model server's base URL and model, optionally a
cloud model, and save. No `.env` file is needed; see [Models](#models).

## Deployment

The Docker image builds the dashboard and serves it together with the API on
one port. Models are set up on the dashboard; `.env` is optional and holds
advanced settings (see [Configuration](#configuration)). The compose file
needs Docker Compose 2.24 or later.

### On a Linux server

1. Install Docker and your local model server (for example Ollama with
   `ollama pull <model>`).
2. Clone the repository and run `docker compose up -d --build`; check that
   `docker compose ps` shows the container as `healthy`.
3. Open the dashboard and create the administrator account.
4. On the **Models** page, set up the local model (and a cloud model if you
   want one), use **Test connection**, and save. `curl
   http://localhost:7632/ready` then returns `{"status":"ready"}`.
5. To stop strangers from signing up, turn **Sign-up** off on the **Users**
   page, or set up [single sign-on](#single-sign-on).
6. For other apps that call the API, create API keys on the dashboard's
   API keys page. Once any account or key exists, the API requires a key.

Instead of building on the server, you can run the image that GitHub Actions
publishes for `linux/amd64` and `linux/arm64`: set
`UGEN_IMAGE=ghcr.io/baihufox3210/ugen-ai-gateway:latest` in `.env`, then run
`docker compose pull && docker compose up -d`. Tags are `latest` (the main
branch), `sha-<commit>`, and `1.2.3` / `1.2` for `v1.2.3` releases. A new
GitHub package is private until you make it public in its package settings
(or run `docker login ghcr.io` on the server first).

The compose file uses host networking, so the container reaches a model
server on the same machine at `localhost` (Ollama's default
`http://localhost:11434/v1` works as is). On Docker Desktop (macOS, Windows),
or to isolate the container, replace `network_mode: host` with the
`ports`/`extra_hosts` lines in the file's comment and use
`http://host.docker.internal:11434/v1` as the local base URL.

To use a routing YAML file (`ROUTING_CONFIG`), mount it into the container,
for example add `- ./routing.yaml:/app/routing.yaml:ro` under `volumes` and
set `ROUTING_CONFIG=/app/routing.yaml`. API keys it names with `api_key_env`
can go in `.env`.

### HTTPS

Sign-in passwords and session tokens travel with every request, so serve the
gateway over HTTPS whenever it is reachable beyond your own network.

- **Tailscale** (private access): `tailscale serve --bg 7632` publishes the
  gateway at `https://<machine>.<tailnet>.ts.net` to your tailnet only.
- **A public domain**: run a reverse proxy such as [Caddy](https://caddyserver.com),
  which obtains certificates automatically. With this `Caddyfile`:

  ```
  gateway.example.com {
      reverse_proxy 127.0.0.1:7632
  }
  ```

  set `GATEWAY_HOST=127.0.0.1` in `.env` so the gateway itself is only
  reachable through the proxy, and allow ports 80 and 443 in your firewall.
  The proxy's `X-Forwarded-For` header is honored, so rate limits still apply
  per client.

### Updating, backups and logs

```sh
git pull && docker compose up -d --build   # update; data is kept
docker compose pull && docker compose up -d   # update, with UGEN_IMAGE set
docker compose logs -f gateway             # follow the logs
```

Without `UGEN_IMAGE` the image is built from the source here, so
`docker compose pull` alone changes nothing. After an update, reload the
dashboard with Ctrl+Shift+R.

Accounts, API keys, settings, saved conversations, usage metadata and the
audit log live in one SQLite file in the `ugen-data` volume, which survives
rebuilds (`docker compose down -v` deletes it). Conversations, provider API
keys and the single sign-on client secret in it are encrypted with
`secret.key`, which is created next to the database on first use (or derived
from `SECRET_KEY` when set). **Back up `secret.key` together with the
database**: without it, saved conversations cannot be read and stored provider
keys must be entered again. To take a consistent backup while the gateway
runs:

```sh
docker compose exec gateway python -c "import sqlite3; sqlite3.connect('/app/data/ugen.db').backup(sqlite3.connect('/app/data/backup.db'))"
docker compose cp gateway:/app/data/backup.db ./ugen-backup.db
docker compose cp gateway:/app/data/secret.key ./ugen-secret.key
```

To restore a backup, copy it in and load it the same way, then restart:

```sh
docker compose cp ./ugen-backup.db gateway:/app/data/restore.db
docker compose exec gateway python -c "import sqlite3; sqlite3.connect('/app/data/restore.db').backup(sqlite3.connect('/app/data/ugen.db'))"
docker compose restart gateway
```

A forgotten administrator password is reset from the server:

```sh
docker compose exec gateway python -m ugen_ai_gateway.cli list-users
docker compose exec gateway python -m ugen_ai_gateway.cli reset-password <username>
docker compose exec gateway python -m ugen_ai_gateway.cli make-admin <username>
```

`reset-password` prints a random password (or takes `--password`) and signs
the account out everywhere.

## Models

Administrators manage model connections on the dashboard's **Models** page;
changes apply to the next request, without a restart.

- **Local model**: any OpenAI-compatible server on your own hardware, such as
  Ollama (`http://localhost:11434/v1`), LM Studio (`http://localhost:1234/v1`),
  vLLM or a llama.cpp server. It answers simple requests and analyzes every
  request for personal data.
- **Cloud model**: any OpenAI SDK-compatible API, for example OpenAI
  (`https://api.openai.com/v1`), Gemini
  (`https://generativelanguage.googleapis.com/v1beta/openai/`), Claude
  (`https://api.anthropic.com/v1/`) or OpenRouter. The switch turns cloud
  routing on or off and keeps the settings either way.
- **Load models** lists the provider's models; **Test connection** sends a
  tiny request with the settings on screen, before saving.
- **Alias** gives a model a public name ([Model aliases](#model-aliases));
  the optional **prices** per million tokens feed the cost and savings figures.
- **Analysis model** (optional): a smaller local model that classifies
  requests, so routing is faster; the local model is used otherwise.
- **Embedding model** (optional): a local model that serves
  [`/v1/embeddings`](#post-v1embeddings).
- **Routing**: *Fast* (rules settle clear cases, the local model the rest,
  `hybrid`) or *Thorough* (the local model judges every request, `llm`); see
  [Routing behavior](#routing-behavior).
- **More models**: add further local or cloud connections, then **routing
  rules** that send matching requests to them. Rules match on complexity, task
  type, personal data (any, or specific types) and request length; they are
  checked from the top and the first match wins, before the default policy.
  Every model is also listed by `GET /v1/models` and can be requested by name.

API keys are encrypted in the gateway's database and never sent back to the
browser; the page shows only their last four characters. Until the page is
saved, the `LOCAL_*` / `CLOUD_*` settings below are used as the starting
values; a `ROUTING_CONFIG` file overrides the page entirely.

## Configuration

Nothing in `.env` is required. Copy `.env.example` to `.env` to change
advanced settings; it documents every one. The main ones:

| Variable | Purpose | Default |
| --- | --- | --- |
| `ALLOW_REGISTRATION` | Allow sign-ups after the first (administrator) account, until changed on the Users page | `true` |
| `SECRET_KEY` | Encrypts stored provider keys; when blank, `secret.key` next to the database is used | blank |
| `ROUTING_CONFIG` | YAML with several providers, models and rules; overrides the Models page ([Routing rules](#routing-rules-and-multiple-models)) | blank |
| `LOCAL_*`, `CLOUD_*` | Starting values for the Models page before it is first saved | blank |
| `SESSION_TTL_DAYS` | How long a dashboard sign-in lasts | `30` |
| `CONVERSATION_STORAGE_MB` | Saved conversations per account, compressed | `200` |
| `GATEWAY_HOST`, `GATEWAY_PORT` | Docker only: listen address and port | `0.0.0.0`, `7632` |
| `UGEN_IMAGE` | Docker only: image to run, e.g. `ghcr.io/baihufox3210/ugen-ai-gateway:latest` | built locally |
| `USAGE_DB_PATH` | SQLite database; empty turns off accounts and usage tracking | `data/ugen.db` (Docker: `/app/data/ugen.db`) |
| `GATEWAY_API_KEY`, `ADMIN_API_KEY` | Optional fixed keys with full access (for scripts); `APP_ENV=production` requires `GATEWAY_API_KEY` | blank |
| `INJECTION_ACTION`, `OUTPUT_PII_ACTION` | Guards ([Guards](#guards)) | `flag`, `off` |
| `FALLBACK_TO_LOCAL`, `FALLBACK_TO_CLOUD` | Fallback when a provider is down ([Resilience](#resilience)) | `true`, `false` |
| `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS` | Requests per client IP per window | `60`, `60` |
| `REQUEST_TIMEOUT`, `MAX_RETRIES` | Provider timeout (seconds) and retries | `300`, `2` |

After changing `.env`, apply it with `docker compose up -d`.

`/health` reports process liveness; `/ready` checks that the local model
endpoint (and configured cloud endpoint) can be reached, caching the result
for 10 seconds, and lists the reasons when it is not ready.

## API

### OpenAI-compatible endpoints

Any OpenAI SDK or tool can use the gateway by pointing its base URL at
`http://localhost:7632/v1`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:7632/v1", api_key="your-gateway-key")
reply = client.chat.completions.create(
    model="ugen-auto",
    messages=[{"role": "user", "content": "Explain what an API gateway does."}],
)
print(reply.choices[0].message.content)
```

#### `POST /v1/chat/completions`

Accepts the standard Chat Completions request and returns the standard
response. `stream: true` returns Server-Sent Events chunks ending with
`data: [DONE]`; `stream_options: {"include_usage": true}` adds a final usage
chunk. `temperature`, `max_tokens`, `max_completion_tokens`, `top_p`, `stop`,
`presence_penalty`, `frequency_penalty`, and `seed` are forwarded to the
selected model. Tool calling (`tools`) and `n > 1` are reserved for a later
version and return HTTP `400`. Image parts in messages are passed through
without being inspected for personal data, so keep them on the local model
when that matters.

The `model` field picks the routing mode:

| `model` | Behavior |
| --- | --- |
| `ugen-auto` (default) | Analyze the request and route automatically |
| `ugen-local` | Always use the local model |
| `ugen-cloud` | Always use the cloud model |
| the local or cloud model name (or its alias) | Same as `ugen-local` / `ugen-cloud` |

Any other model name returns HTTP `404` with code `model_not_found`. Every
request is still analyzed and redacted locally, whichever mode is chosen.

The routing decision is reported in response headers (`X-UGen-Provider`,
`X-UGen-Model`, `X-UGen-Route-Reason`, `X-UGen-Redacted-Values`) and in an extra
`ugen` object on the response (or on the final stream chunk) with the full
`routing` analysis and `performance` timings. Errors use the OpenAI error shape:
`{"error": {"message": "...", "type": "...", "code": ...}}`.

#### `GET /v1/models`

Lists `ugen-auto`, `ugen-local`, `ugen-cloud` (when cloud routing is enabled),
`ugen-embed` (when an embedding model is set), and the configured model names.

#### `POST /v1/embeddings`

Standard embeddings request (`input` as a string or list, optional
`encoding_format` and `dimensions`), answered by the local embedding model set
on the Models page. Embeddings never go to the cloud, so the text is not
redacted. `model` may be `ugen-embed` or the embedding model's name; without
an embedding model the endpoint returns HTTP `404`.

### Accounts

The dashboard requires signing in. The first account created becomes the
administrator; later accounts are members. Administrators manage models,
privacy rules, accounts and API keys, export the audit log and see every
request's usage; members see only their own.

The **Users** page (administrators) lists every account with this month's
requests and cloud cost, and can make or remove administrators, disable an
account (which signs it out), reset a password (shown once), delete an
account, set a **monthly cloud budget** per person (once reached, that
person's requests stay on the local model; `routing.rule` is `user.budget`),
and open or close **Sign-up**. Account changes are recorded in the audit log.
Everyone can change their password, profile picture and theme on the
**Account** page.

- `GET /v1/ugen/auth/status` tells the sign-in screen whether accounts are
  enabled, whether any exist and whether sign-up is open.
- `POST /v1/ugen/auth/register` and `POST /v1/ugen/auth/login` take
  `{"username": "...", "password": "..."}` and return
  `{"token": "ugs_…", "expires_at": ..., "user": {...}}`. Usernames are 3–32
  letters, digits, dots, dashes or underscores (case-insensitive); passwords
  are at least 8 characters. Sign-in and sign-up are limited to 10 attempts
  per minute per IP.
- `GET /v1/ugen/auth/me` returns the signed-in user and
  `POST /v1/ugen/auth/logout` ends the session.
- `PUT /v1/ugen/auth/me/avatar` with `{"image": "data:image/webp;base64,..."}`
  sets the profile picture (PNG, JPEG or WebP, at most 200 KB; the dashboard
  crops and resizes it to 160×160 first) and `{"image": null}` removes it.
- `GET`/`PUT /v1/ugen/auth/me/instructions` with `{"text": "..."}` (at most
  4,000 characters; empty removes them) reads or sets the account's custom
  instructions. See [System prompts](#system-prompts).
- `PUT /v1/ugen/auth/me/password` with `{"current_password": "...",
  "new_password": "..."}` changes the password and signs out other browsers.
- `DELETE /v1/ugen/auth/me` with `{"password": "..."}` (or `{"username":
  "..."}` for an account that only uses single sign-on) deletes the account,
  its sessions, and its request metadata and feedback. Audit entries stay
  (they never name the user, and removing them would break the hash chain).
  If the last administrator leaves, the oldest remaining account is promoted.

- `GET /v1/ugen/users`, `PATCH /v1/ugen/users/{id}` (`is_admin`, `disabled`,
  `monthly_budget_usd`), `POST /v1/ugen/users/{id}/reset-password`,
  `DELETE /v1/ugen/users/{id}`, and `GET`/`PUT /v1/ugen/settings`
  (`allow_registration`) back the Users page.

Passwords are stored as salted scrypt hashes and session tokens as SHA-256
hashes, in the same SQLite database as usage (`USAGE_DB_PATH`). Without a
database, accounts are off and the dashboard opens without sign-in.

### Single sign-on

People can sign in with Google, or with any OpenID Connect provider such as
Microsoft Entra ID, Okta, Keycloak or Authentik.

**Google**: on the **Users** page choose **Google** under *Single sign-on*,
then:

1. In [Google Cloud Console → APIs & Services →
   Credentials](https://console.cloud.google.com/apis/credentials), set up the
   OAuth consent screen if asked (*Internal* limits sign-in to your Google
   Workspace; *External* allows any Google account, and while the app is in
   testing only the test users you add).
2. **Create credentials → OAuth client ID**, application type **Web
   application**, and add the page's **Callback URL** (for example
   `https://gateway.example.com/api/v1/ugen/auth/oidc/callback`) under
   **Authorized redirect URIs**. Google requires `https://` except for
   `localhost`.
3. Paste the **Client ID** and **Client secret**, optionally limit sign-in to
   your **email domains**, turn it on and save. The sign-in screen then shows
   **Sign in with Google**.

**Other providers**: choose **Other OpenID provider** on the **Users** page:

1. Copy the **Callback URL** shown there and register it with your provider
   as a redirect URI of a new web application (OAuth client).
2. Enter the provider's **issuer URL** (for example
   `https://accounts.google.com`, or
   `https://login.microsoftonline.com/<tenant>/v2.0`), the **client ID** and
   **client secret**, and use **Check provider**.
3. Optionally limit sign-in to **allowed email domains**, and choose whether
   an account is created the first time someone signs in. Turn it on and save.

The sign-in screen then shows the button. The gateway uses the authorization
code flow with PKCE, and checks the ID token's issuer, audience, nonce and
expiry. Accounts are linked by the provider's subject identifier; usernames
come from the provider's preferred username or email. An SSO account can set
a password on the Account page to also sign in with it.

**Linking an existing account**: on the **Account** page, *Link Google
account* (or *Link single sign-on*) connects the provider to the account you
are signed in to, so an administrator who registered with a password can then
sign in with Google too. If that Google account already signed in here once,
it created a separate account: delete that one on the **Users** page first.
*Unlink* is available to accounts that also have a password. The client secret is
encrypted like provider keys. Serve the gateway over HTTPS, and if a proxy
changes the address the gateway sees, set the callback URL override under
**Advanced**.

### Authentication and API keys

Requests authenticate with `Authorization: Bearer <key>` (what OpenAI SDKs
send) or `X-API-Key`. Accepted credentials:

- `GATEWAY_API_KEY` and `ADMIN_API_KEY`: no restrictions, and stats cover
  every request.
- A dashboard session token (`ugs_…`) from signing in.
- API keys created by an administrator: one per team or application, each
  with its own policy, and stats and feedback limited to its own requests.

The gateway runs open (no key needed) only while there is no
`GATEWAY_API_KEY`, no `ADMIN_API_KEY`, no account and no API key has ever been
created. Creating the first account or key locks it, and deleting every
account or revoking every key does not reopen it.

Manage keys from the dashboard settings as an administrator, or with
`ADMIN_API_KEY` (or `GATEWAY_API_KEY` when no admin key is set). With none of
those, the key endpoints work only until the first key or account exists:

```sh
curl -X POST localhost:7632/v1/ugen/keys -H 'X-API-Key: <admin key>' \
  -H 'Content-Type: application/json' \
  -d '{"name": "marketing", "allow_cloud": true, "monthly_budget_usd": 20, "rate_limit_per_minute": 60}'
```

The response includes the secret `key` (`ugk_…`) once; only its SHA-256 hash is
stored. `GET /v1/ugen/keys` lists keys with this month's request count and
cost, `DELETE /v1/ugen/keys/{id}` revokes one, and `GET /v1/ugen/whoami`
reports what the presented key allows. A key with `allow_cloud: false`, or
whose monthly cloud spend has reached `monthly_budget_usd`, is answered by the
local model instead (`routing.rule` is `key.local_only` or `key.budget`).

Requests are rate limited per API key (`rate_limit_per_minute`) or, for the
other keys and open mode, per client IP (`RATE_LIMIT_REQUESTS` per
`RATE_LIMIT_WINDOW_SECONDS`); an exceeded limit returns HTTP `429`. The
limiter is in-memory and per process behind a small `RateLimiter` interface,
so running several workers needs a shared store, such as Redis, behind that
interface.

In the dashboard, the account button in the sidebar opens the Account page
(profile picture, password, theme, sign out, delete account) and, for
administrators, the gear icon opens the Users, Models, Privacy and API keys
pages. On the API keys page an administrator creates, lists and revokes keys
and exports the audit log.

### Dashboard endpoints

These power the bundled dashboard and expose more detail than the OpenAI API.

- `GET /health` returns `{"status":"ok"}` when the process is running.
- `GET /ready` checks that the configured model endpoints can be reached.
- `GET /v1/ugen/config` returns the configured model names and privacy
  settings (no secrets).
- `POST /v1/ugen/chat` takes `messages`, `temperature`, and
  `provider_selection` (`auto`, `local`, or `cloud`) and returns:

```json
{
  "model": "qwen2.5:7b",
  "content": "An API gateway ...",
  "finish_reason": "stop",
  "provider": "local",
  "routing": {
    "complexity": "simple",
    "task_type": "general",
    "contains_personal_data": false,
    "personal_data_types": [],
    "provider": "local",
    "reason": "Local model selected by the default routing policy.",
    "redacted_values": 0
  },
  "usage": {
    "analysis": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250},
    "generation": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
  },
  "performance": {"analysis_ms": 500.0, "generation_ms": 800.0, "total_ms": 1300.0}
}
```

- `POST /v1/ugen/chat/stream` takes the same body and emits newline-delimited
  JSON events: `analysis_started`, `analysis_completed`, `routed`,
  `generation_started`, one `delta` event per generated text chunk
  (`{"type":"delta","content":"..."}`), and finally `result` with the body
  above. A failure emits an `error` event instead of `result`, even after some
  deltas were sent.

- `GET /v1/ugen/providers` reports each provider's circuit-breaker state.
- `GET /v1/ugen/stats?days=7&tz_offset_minutes=480` returns totals for the
  period: completed and failed requests, local/cloud split, tokens, generation
  speed, cost, savings, personal-data and fallback counts, per-rule counts,
  feedback, and daily counts bucketed in the given UTC offset.
- `GET /v1/ugen/requests?limit=50&before=<created_at>` lists recent request
  metadata, newest first.
- `POST /v1/ugen/feedback` with `{"request_id": "...", "rating": 1}` (or `-1`)
  records feedback on an answer; `request_id` is the `id` of the chat response.
- `POST /v1/ugen/memory` with `{"previous": "...", "messages": [...]}` folds
  older messages into a conversation memory with the local model and returns
  `{"memory": "..."}` (see [Conversation memory](#conversation-memory)).
- `GET`/`PUT /v1/ugen/models`, `POST /v1/ugen/models/test` and
  `POST /v1/ugen/models/list` back the Models page; `GET`/`PUT
  /v1/ugen/privacy` and `POST /v1/ugen/privacy/test` the Privacy page; and
  `GET`/`PUT /v1/ugen/sso` and `POST /v1/ugen/sso/test` single sign-on.

### Using the dashboard

- **Conversations** are saved to your account on the gateway and follow you
  to every browser and device you sign in to (see
  [Saved conversations](#saved-conversations)). They are listed in the sidebar
  with search (titles and message text). Double-click a title or use ✎ to rename,
  and **Export** a conversation as Markdown or JSON.
- **Stop** ends an answer while it streams. Each answer can be copied or
  regenerated, and each of your messages edited and sent again. Code blocks
  are highlighted and have a copy button; Markdown tables and LaTeX math
  (`$…$`, `$$…$$`) are rendered.
- **Theme**: System, Light or Dark, on the Account page.
- **Custom instructions** on the Account page are added to all your
  conversations; administrators set a **system prompt** for everyone at the
  top of the Models page. See [System prompts](#system-prompts).
- **Share** a conversation as a link or chat in it together; see
  [Sharing](#sharing).

### Saved conversations

With accounts, each person's conversations are stored on the gateway:

- **Encrypted**: every conversation, title included, is compressed and
  encrypted with `secret.key` / `SECRET_KEY` before it is written. Only the
  account that saved it can read it; administrators cannot.
- **Synced**: changes are saved within a second; other open browsers pick
  them up when their window regains focus and every 30 seconds. The sidebar
  shows *synced across devices*, *saving…* or *not saved, retrying*. When the
  same conversation is changed on two devices at once, the last save wins.
- **Moved automatically**: history saved in a browser by an older version is
  uploaded the first time its owner signs in there, then removed from the
  browser.
- **Limits**: 5 MB of text per conversation and `CONVERSATION_STORAGE_MB`
  (default 200, measured compressed) per account. Deleting an account deletes
  its conversations.
- `GET /v1/ugen/conversations?since=<server_time>` returns conversations
  changed since an earlier response, plus every id; `POST
  /v1/ugen/conversations/sync` with `{"save": [...], "delete": [...]}` saves
  and deletes. Both need a dashboard session.

Without accounts (no database), conversations stay in the browser.

### Sharing

The **Share** button above a conversation offers two things:

- **Share a link**: a snapshot of the messages and answers (not the
  conversation's memory or routing details), opened with `/?share=<id>`.
  Signed-in people who open it get **their own copy** in their conversation
  list and continue it right away; you never see what they add, and they do
  not see what you add later (opening the link again reopens their copy).
  Choose who can open it: *people signed in here* (default) or *anyone with
  the link* (people without an account can read it and sign in to continue).
  *Update link* refreshes the snapshot under the same address and *Stop
  sharing* turns it off.
- **Chat together**: invite accounts by username. The conversation becomes a
  shared chat that every member sees in their list (marked 👥) and can
  continue; each message shows who sent it, and other members' messages
  appear within a few seconds. Members' changes are merged message by message,
  so two people writing at once do not overwrite each other. Requests are
  analyzed, routed and counted against whoever sends them. The owner adds and
  removes members; a member deleting it from the list leaves it, and the owner
  deleting it deletes it for everyone. *Clear all* never touches shared chats.

Snapshots and shared chats are encrypted like saved conversations. API:
`POST /v1/ugen/shares` (`conversation`, `visibility`), `GET /v1/ugen/shares`,
`GET`/`DELETE /v1/ugen/shares/{id}`; `POST /v1/ugen/shared-chats`
(`conversation`, `members`), `PATCH /v1/ugen/shared-chats/{id}` (`add`,
`remove`), `DELETE /v1/ugen/shared-chats/{id}`.

### Conversation memory

Conversations keep their context without sending the whole history. The
model receives the conversation's **memory** plus only the last two exchanges
word for word. Once three more older exchanges have built up (or about 4,000
characters of them), the dashboard asks the gateway to fold them into the
memory: the local model writes down the facts, decisions, preferences, names
and open tasks worth keeping, merged with the memory so far. So a request
carries at most the memory and four exchanges, however long the conversation,
and the summary is rewritten every few messages rather than on every one.
Exchanges that are very long on their own are folded too.

The memory is sent as a system message, so it is analyzed and redacted like
any other text. It never leaves the local model while it is written, and is
saved with the conversation. The full history stays visible in the
conversation; a marker shows how many earlier messages the memory covers.

### System prompts

Two optional prompts go ahead of every conversation, as one system message:

1. The **system prompt** an administrator sets at the top of the Models page
   (`GET`/`PUT /v1/ugen/system-prompt` with `{"text": "..."}`, at most 8,000
   characters). It applies to every request, from the dashboard and the
   OpenAI-compatible API alike, for house rules such as language, tone or
   topics to decline. The audit log records each change with its length only.
2. Each account's **custom instructions**, set on the Account page: who they
   are or how they like answers. They apply to that account's requests while
   it is signed in, never to API keys (an app writes its own prompts). They
   are stored encrypted like saved conversations.

Then come the conversation memory and the client's own system messages. All
of it is analyzed and redacted like any other text, so keep personal data out
of the system prompt: a phone number there would be replaced by a placeholder
on its way to a cloud model, and could keep requests local under privacy
rules.

### Guards

**Prompt injection.** User messages are checked for common injection
phrasings in English and Chinese ("ignore all previous instructions",
「忽略之前的指示」, requests to reveal the system prompt, "developer mode",
sending keys to a URL). This is a heuristic tripwire, not a guarantee.
`INJECTION_ACTION` decides what happens:

| Value | Behavior |
| --- | --- |
| `flag` (default) | Record the signals (`routing.injection_signals`, a `guard` stream event, usage and audit logs) and continue |
| `local` | Also keep the request on the local model |
| `block` | Reject with HTTP `400` before any model is called |
| `off` | Skip the check |

**Personal data in answers.** The reply is scanned for personal data that the
user did not provide, such as a phone number or key the model produced on its
own. It is off by default (`OUTPUT_PII_ACTION=off`): personal data is hidden
from models, not from the person reading the answer, and placeholders are
restored in replies. `redact` replaces such data with a `[LABEL]` marker and
`flag` only records it. Values from the request are left
alone. Names and places are not checked in answers (answers mention them
all the time, and the model cannot leak one it was never given); only
structured data such as numbers, keys and cards is. When streaming, text is released only at points no value can span (the
last 24 characters are held until the next safe break), so a value is never
sent in pieces before it can be checked. Findings appear in
`routing.output_findings`.

### Audit log

Every request appends an entry to an audit log in the usage database: the same
metadata as usage tracking (never message text or values), chained so that
each entry's SHA-256 covers the previous entry's hash. Changing, deleting, or
reordering an entry breaks verification from that point on.

- `GET /v1/ugen/audit?since=<unix time>&limit=1000&format=json|csv` exports it:
  entries from `since` onwards, or without `since` the newest `limit` entries,
  oldest first either way.
- `GET /v1/ugen/audit/verify` recomputes the chain and returns
  `{"ok": true, "entries": N, "first_invalid_seq": null}`.

Both need an administrator account, or `ADMIN_API_KEY` (or `GATEWAY_API_KEY`
when no admin key is set), like key management.

### Usage tracking

Each request's metadata is stored in SQLite at `USAGE_DB_PATH` (default
`data/ugen.db`; empty disables tracking): routing decision, provider and model,
matched rule, task type, complexity, analysis source, which personal-data
types were found and how many values were redacted, fallback, token counts,
timings, and cost. Usage records never contain message text or personal-data
values; the only message text on the server is in [saved
conversations](#saved-conversations), encrypted.

Cost uses the per-million-token prices in the routing configuration. Savings
compare it with the same tokens on the default cloud model, so answers kept on
the local model count as saved. The dashboard shows these totals, and its
👍/👎 buttons send feedback.

Provider usage values depend on what the upstream model API reports; missing
usage is `null`. `analysis_ms` measures the local analysis request,
`generation_ms` the generation request, and `total_ms` the whole operation.

## Routing behavior

Each request is analyzed locally before routing. The Models page's **Routing**
setting (*Fast* is `hybrid`, *Thorough* is `llm`; `ANALYSIS_MODE` before the page
is saved) controls how:

| Mode | Behavior |
| --- | --- |
| `hybrid` (default) | The rules decide when they are sure and the request stays local; the local model analyzes everything else |
| `llm` | Always ask the local model |
| `rules` | Rules only, never call the model |

**How difficulty is judged.** Rules settle a request only when the signal is
unambiguous: small talk and thanks, "what is X" with nothing more asked, short
rewrites, translations and wishes (simple), or explicit design, proof,
algorithm or debugging work (complex). Words like "design" or 比較 alone
decide nothing ("幫我設計一句生日祝福" is simple, "幫我設計一個會員系統"
complex). Everything else is left to the local model, whose prompt defines the
levels by what a 7-9B model answers well, with examples. When the rules are
sure, the model's opinion does not override them. A short follow-up
("第二點再詳細一點", "continue") is judged together with the request before it.
`src/ugen_ai_gateway/analysis/complexity_cases.json` holds 600 labeled
Chinese and English requests from two separately written batches, many
deliberately tricky; the tests require that the rules are never wrong about
local versus cloud on the ones they settle (about a third). To measure the
whole judgement with your own analysis model, run
`python -m ugen_ai_gateway.cli check-routing` (with Docker:
`docker compose exec gateway python -m ugen_ai_gateway.cli check-routing`); it
prints the share judged right and lists the misses. It calls the analysis
model for about two thirds of the requests, so it takes a few minutes.

Rule-based analysis only detects the structured personal data listed under
[Personal-data handling](#personal-data-handling); names and addresses need the
model. That is why `hybrid` only skips the model for requests that stay on this
machine, and why `rules` is not recommended when cloud routing is enabled.

Model analysis results are cached in memory for identical request text
(`ANALYSIS_CACHE_SIZE`, default `256` entries, `0` disables;
`ANALYSIS_CACHE_TTL_SECONDS`, default `600`). Cache keys are SHA-256 hashes,
and cached results never leave the process. `routing.analysis_source` and the
`analysis_completed` stream event report `llm`, `rules`, or `cache`.

Complexity is judged from the latest user message, so a short follow-up in a
long conversation is not sent to the cloud just because of the history, while
personal data is checked in every user and system message. Each message's
analysis is cached, so earlier turns are not analyzed again. When cached
results have expired (for example after a break in a long conversation), the
latest message is analyzed on its own while all the older ones share a single
call that only looks for personal data, run alongside it: however long the
conversation, a request needs at most two model calls, and the values found
are cached per message again.

Each request is classified simple, moderate or complex (see *How difficulty
is judged* above). By default moderate and complex tasks use the cloud provider
when cloud routing is enabled; the Models page's **What goes to the cloud**
setting (`CLOUD_FROM=complex` before the page is saved) sends only complex ones,
which suits a capable local model. Simple tasks stay local. If cloud routing is disabled, requests stay local. The
returned `routing` object reports the estimated complexity, task type, detected
personal-data types, selected provider, and the reason for that decision.

### Routing rules and multiple models

For more than one local and one cloud model, point `ROUTING_CONFIG` at a YAML
file. [`routing.example.yaml`](routing.example.yaml) documents every field:

- `providers`: each OpenAI-compatible endpoint, whether it is `local` or `cloud`,
  its API key (`api_key_env` reads it from the environment), and its models
  with per-million-token prices for cost reporting. A provider whose key is
  missing is skipped with a warning, and rules pointing at it are ignored.
- `defaults`: the local and (optional) cloud model used for `ugen-local`,
  `ugen-cloud`, fallbacks, and requests no rule matches.
- `analysis`: the model that analyzes requests. It sees unredacted text, so it
  must be a local provider; the file is rejected otherwise.
- `rules`: checked in order, first match wins. A rule can match on
  `complexity`, `task_type`, `contains_personal_data`, `personal_data_types`
  (any of), `min_chars`, and `max_chars`, and routes to a provider and model.
- `privacy.never_cloud`: personal-data types that always keep the request on
  the local default model, overriding rules and explicit `ugen-cloud` requests.

Every configured model appears in `GET /v1/models`; requesting one by name pins
that model, still subject to `never_cloud`. `routing.provider_name` and
`routing.rule` in responses report which provider and rule were used
(`privacy.never_cloud` when the privacy rule applied).

Without `ROUTING_CONFIG`, the `LOCAL_*` and `CLOUD_*` variables define one local
and one cloud provider, with moderate and complex requests going to the cloud.

### Model aliases

The **Alias** fields on the Models page (or `alias:` on a model in the routing
YAML) give a model a public name. The alias replaces the real model
name everywhere clients look: the dashboard, `GET /v1/models`, the `model`
field and `X-UGen-Model` header of responses, stream events and usage records.
Requests may use either the alias or the real name as `model`; the provider is
always called with the real name. When names overlap, the first matching
model in the configuration wins.

## Resilience

Provider calls are retried with exponential backoff on connection errors, HTTP
`429`, and `5xx` responses (`MAX_RETRIES`, default `2`); a stream is only
retried before any text has been sent.

If the routed provider is still unavailable, automatically routed requests fall
back to the other provider before any text has been sent:

| Variable | Behavior | Default |
| --- | --- | --- |
| `FALLBACK_TO_LOCAL` | Cloud unavailable: answer with the local model, using the same redacted messages | `true` |
| `FALLBACK_TO_CLOUD` | Local unavailable: answer with the cloud model, only when the local model analyzed the request and found no personal data | `false` |

Requests that chose a provider explicitly (`ugen-local`, `ugen-cloud`, or
`provider_selection`) never fall back. A fallback is reported as a `fallback`
stream event, in `routing.fallback_from`, and in the `X-UGen-Fallback-From`
header.

Each provider has a circuit breaker: after `CIRCUIT_FAILURE_THRESHOLD`
consecutive outages (default `3`) the provider is skipped for
`CIRCUIT_COOLDOWN_SECONDS` (default `30`), then a single request probes it
again. `GET /v1/ugen/providers` reports each breaker's state (`closed`, `open`,
or `half_open`).

## Personal-data handling

Before sending messages to the generation provider, the gateway redacts the
following, detected locally with regular expressions (and checksums where the
format has one):

| Type | Examples |
| --- | --- |
| `email` | `alex@example.com` |
| `taiwan_mobile`, `taiwan_landline` | `0912-345-678`, `(02)2345-6789`, `+886-2-2345-6789` |
| `taiwan_id`, `taiwan_resident_certificate` | National ID, old and new resident certificate numbers (checksum validated) |
| `taiwan_ubn` | 統一編號 (checksum validated) |
| `passport`, `nhi_card` | Passport and 健保卡 numbers |
| `credit_card` | Card numbers (Luhn validated) |
| `api_key`, `jwt`, `private_key`, `password` | `sk-…`, `AKIA…`, `ghp_…`, JWTs, PEM private keys, `password=…` |

Short numeric identifiers (統一編號, passport, 健保卡) only count when a keyword
such as `統編`, `護照`, or `健保卡號` comes right before them, so ordinary 8-digit
amounts or dates are left alone. For passwords and those keyword-led values,
only the value is replaced and the keyword stays readable. The analysis model
can also report additional exact personal-data values, which are redacted from
text message content. Matching works inside Chinese text without spaces.

Names and places are also detected with
[Presidio](https://microsoft.github.io/presidio/) named-entity recognition
(spaCy models), so "王小明", "台北市" or "John Smith in Seattle" are redacted
even when the analysis model misses them. The Docker image includes it for
Chinese and English and turns it on (`PII_NER=presidio`,
`PII_NER_LANGUAGE=zh,en`, `PII_NER_SPACY_MODEL=zh_core_web_sm,en_core_web_sm`):
each language's model only runs on text in its script. Set `PII_NER=none` in
`.env` to turn it off, or build with `--build-arg PII_NER=none` for an image
about 300 MB smaller without it. The small models catch common names and
places but not every one; the analysis model remains the main detector.
Outside Docker:

```sh
uv sync --extra ner
uv pip install https://github.com/explosion/spacy-models/releases/download/zh_core_web_sm-3.8.0/zh_core_web_sm-3.8.0-py3-none-any.whl \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

then set the three variables above (`PII_NER_SCORE_THRESHOLD`, default `0.5`,
tunes how sure a match must be). The spaCy model takes milliseconds per scan, so with it on the
gateway redacts input on a worker thread and checks streamed replies once per
sentence (rather than per token), also on a worker thread; other requests keep
streaming meanwhile. Replies then arrive a sentence at a time.

Redaction is reversible. Each value is replaced with a numbered placeholder such
as `[EMAIL_1]`; the same value gets the same placeholder across every message in
the request, and a short system instruction asks the model to keep placeholders
as written. The placeholder-to-value mapping exists only in memory for that one
request. When the reply comes back, placeholders are replaced with the original
values (also in streamed deltas, even when a placeholder is split across
chunks), so the user sees a complete answer while the model never saw the data.
The response's `routing.redacted_values` reports how many values were replaced.
Redaction and restoring are always on, for the local model as well as the
cloud; there is nothing to switch on.

### Privacy page

Administrators can extend detection on the dashboard's **Privacy** page:

- **Custom terms**: keyword lists (one term per line) or regular expressions
  for things that must not reach the cloud as written, such as project code
  names, client names or internal hostnames. Matches are replaced with
  placeholders like `[PROJECT_1]` and restored in the reply. A term can also
  keep any request containing it on the local model.
- **Try it** shows what the cloud would receive for sample text, using the
  rules on screen before they are saved.

Every request is redacted automatically. Common
patterns are detected locally as a backstop if the analysis model misses them.
The current analysis and redaction flow applies to text; image or other
non-textual content is not inspected for personal data.

Name, address, and other free-form data do not have a universal deterministic
pattern. Detection of those values depends on the analysis model; if the model
reports personal data it cannot point to exactly (a type without the values),
the request is kept on the local model (`routing.rule` is
`privacy.unredactable`) and is only refused when no local model is available.
With name and place detection on (`PII_NER=presidio`, the Docker default),
names and places are looked for in every message before anything is sent,
so such a claim no longer keeps a request local: small analysis models raise
it out of caution even for plain technical requests, and it names nothing
that could be redacted. Values the model does point to are still redacted.
PII detection is a safeguard,
not a guarantee that every sensitive value will be recognized.

## Security

- Serve the gateway over HTTPS beyond your own network ([HTTPS](#https)).
- Responses carry a strict Content Security Policy (scripts only from the
  gateway itself), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`
  and a no-referrer policy.
- Passwords use scrypt; session tokens and API keys are stored only as
  SHA-256 hashes; provider keys and the SSO client secret are encrypted with
  `secret.key` / `SECRET_KEY`.
- Sign-in attempts are rate limited per IP, and account, key, model, privacy
  and sign-on changes are recorded in the tamper-evident audit log.

## Limits and errors

- At most 64 messages per request.
- At most 32,000 text characters across the request.
- Invalid request data returns HTTP `422`; a request exceeding the gateway's
  input limit returns HTTP `413`.
- Missing provider configuration or an unavailable provider returns HTTP `503`.
- Invalid or missing gateway API keys return HTTP `401`; rate-limited requests
  return HTTP `429`.
- Provider timeouts return HTTP `504`.
- Other upstream provider errors return HTTP `502`.

## Troubleshooting

**Is the new version running?** `git log -1 --oneline` shows the source the
image was built from. With `UGEN_IMAGE`, the image's commit is:

```sh
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$UGEN_IMAGE"
```

**Why did a request stay local?** The footer under each answer says why:

| Footer shows | Reason | What to do |
| --- | --- | --- |
| `Complexity: simple` | Judged simple (see [How difficulty is judged](#routing-behavior)) | Expected; or choose **Cloud** below the message box for this one |
| `Complexity: moderate` with **Complex only** set | Only complex requests go to the cloud | Models page → *What goes to the cloud* |
| `Cannot redact safely: kept local` | The analysis model reported personal data it could not point to | Turn on name and place detection (`PII_NER=presidio`); then this no longer blocks the cloud |
| `Privacy policy: kept local` | A privacy rule marked the data as never leaving | Privacy page |
| `Budget used up` / `Key restriction` | A budget or an API key's limit | Users or API keys page |
| `Possible injection: kept local` | The injection guard (`INJECTION_ACTION=local`) | Rephrase, or set the guard to `flag` |
| No such note, `Local` | No cloud model, cloud turned off, or **Local** chosen | Models page; choose **Auto** below the message box |

**How well does routing judge requests?** Run
`docker compose exec gateway python -m ugen_ai_gateway.cli check-routing`:
it judges 600 labeled requests with your models and lists the misses.

**Answers contain `[LOCATION]` or `[PERSON_NAME]`.** An older `.env` may still
set `OUTPUT_PII_ACTION=redact`; remove it (the default `off` masks data only
on its way to a model, never in answers).

## Development

Requirements: Python 3.13 or later, [uv](https://docs.astral.sh/uv/) and
Node.js 22.

```sh
uv sync
uv run ugen-ai-gateway                     # API on http://localhost:7632
cd frontend && npm install && npm run dev  # dashboard on http://localhost:5173
```

The development dashboard reloads as its code changes and proxies `/api` to
the API on port `7632` (set `UGEN_BACKEND_PORT` for another port). When
`frontend/dist` exists (after `npm run build`), the API server also serves the
dashboard at `/`, as the Docker image does.

GitHub Actions runs these checks on every push and pull request
(`.github/workflows/ci.yml`) and publishes the Docker image from `main` and
`v*` tags (`.github/workflows/docker.yml`). Run them locally with:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
cd frontend && npm run build
```

## Contributing

Bug reports and pull requests are welcome. Before opening a pull request, run
the test suite and lint checks above. For larger changes, open an issue first
to discuss the proposed behavior.

## License

This project is licensed under the [MIT License](LICENSE).
