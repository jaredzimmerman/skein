# skein: plan

## Goal
A self-hosted server that lets several consumer AI assistants (Claude, Meta Muse, others) hand
tasks and questions to each other through one shared queue. The user picks one member as the
**point** (lead); every task and ask goes through the point. First version done = the server runs,
every rule below is enforced and tested over both MCP and REST, a CLI manages members and the
point, and `agents/` has a folder per assistant explaining how it joins and what to do as point
or member.

Origin: a private two-party bridge (Claude hands tasks to Meta Muse, Muse asks Claude questions)
built as `muse_queue.py` (SQLite) + `bridge.py` (FastMCP, bearer auth, REST mirror). skein
generalizes it to N members. There is NO per-task human approval: assistants hand off directly.

## Why a queue (framing)
Consumer assistants cannot call each other. They act only when someone talks to them or on a
schedule, and reach out only through connectors. So each assistant gets its own connector into a
shared queue and checks it. Routing goes through the point: members never address each other.

## Architecture
```
assistant ──(MCP streamable HTTP or REST, Bearer <member token>)──▶ app.py (Starlette)
   auth middleware: token → member name (disabled members rejected)
   ├─ /mcp       mcp_tools.py  (FastMCP, stateless, JSON responses)  ─┐
   └─ /api/...   rest.py                                              ├─▶ service.py (ALL rules + authz) ─▶ store.py (SQLite)
                                                                      ─┘
CLI (cli.py) ──▶ service.py admin functions ──▶ store.py      (members, tokens, point; no network)
```
- `store.py`: SQLite only, no rules. `service.py`: the single choke point for authorization,
  routing and state transitions. Transports never touch the store directly and never re-implement
  a rule.
- One process serves all members. Members and the point live in the DB, so `skein point set` /
  `skein member add` take effect without a restart.

## Contracts (frozen; changes need a ledger `decision`)

### C1 Data model (`src/skein/store.py`)
SQLite, WAL, `PRAGMA user_version = 1`, `sqlite3.connect(path, timeout=10)`, one connection per
operation (safe across threads/processes). Times are unix floats in the DB, ISO-8601 `Z` strings in
every API output (`None` stays `null`).
```
members(name TEXT PRIMARY KEY, display TEXT NOT NULL, token_sha256 TEXT UNIQUE NOT NULL,
        created REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, last_seen REAL)
meta(k TEXT PRIMARY KEY, v TEXT)                       -- k='point' → member name
tasks(id TEXT PRIMARY KEY,            -- 't_' + 10 hex
      title TEXT NOT NULL, body TEXT NOT NULL, assignee TEXT NOT NULL, created_by TEXT NOT NULL,
      status TEXT NOT NULL,           -- queued|claimed|needs_input|done|failed|cancelled
      created REAL, claimed REAL, updated REAL, finished REAL,
      result TEXT, notes TEXT NOT NULL DEFAULT '[]',   -- JSON list of {t, from, text}, last 50 kept
      seen_by_point INTEGER NOT NULL DEFAULT 1)
asks(id TEXT PRIMARY KEY,             -- 'a_' + 10 hex
     asker TEXT NOT NULL, question TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
     task_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,   -- open|answered
     created REAL, answered REAL, answer TEXT, answered_by TEXT,
     seen_by_asker INTEGER NOT NULL DEFAULT 0)
```
Limits (reject over-limit with `invalid`, except notes/results which are truncated as stated):
title ≤200 (truncate), instructions ≤8000 (reject), result ≤50000 (truncate), note ≤2000
(truncate), question ≤4000 (reject), context ≤8000 (truncate), answer ≤50000 (truncate). Empty
title/instructions/result/note/question/answer → `invalid`. List limits clamp to 1..100; inbox
clamps to 1..25. Amend window: done/failed tasks accept notes, a re-submitted result and
follow-ups for 7 days after `finished`.

### C2 Service (`src/skein/service.py`, errors in `src/skein/errors.py`, tokens in `src/skein/tokens.py`)
`class SkeinError(Exception)` with `.code` ∈ {`not_found`, `forbidden`, `invalid`, `conflict`} and
a human message. `class Skein(store)`. Every member method takes `caller: str` (an authenticated,
enabled member name) first and returns plain dicts (JSON-safe). Unknown-or-not-yours tasks/asks
raise `not_found` (never leak existence to members).

Task dict: `{id, title, assignee, created_by, status, created, claimed, updated, finished}` plus,
when full, `{instructions, notes, result}`. Ask dict: `{id, asker, question, context, task_id,
status, created, answered, answer, answered_by}`.

Task status flow: `queued → claimed → done | failed | needs_input`; `needs_input → queued` via the
point's `follow_up`; any open state → `cancelled` via the point. Open = queued|claimed|needs_input.
Final = done|failed|cancelled. Cancelled tasks accept nothing. Setting done/failed/needs_input sets
`seen_by_point = 0`.

All members:
- `whoami(caller)` → `{member, display, role: "point"|"member", point, point_display}`; when the
  caller is the point, also `members: [{name, display, last_seen, disabled, open_tasks}]`.
- `inbox(caller, limit=10)` → updates `last_seen`. Member: `{role:"member", point, tasks: [open
  tasks assigned to caller with status queued|claimed, oldest first, full], answers: [asks by
  caller with status answered and seen_by_asker=0 — then mark them seen], checked_in_at}`.
  Point: `{role:"point", asks: [open asks, oldest first], needs_input: [needs_input tasks, full],
  finished: [done/failed tasks with seen_by_point=0, full — then mark seen], checked_in_at}`.
- `get_task(caller, task_id)`: member → only tasks assigned to them; point → any. Full dict.
- `claim(caller, task_id)`: assignee only; status must be queued|claimed → claimed (keep the first
  `claimed` time).
- `note(caller, task_id, text)`: assignee or point; appends `{t, from: caller, text}`.
- `submit(caller, task_id, result, status="done")`: assignee only; status ∈ done|failed|needs_input.
  If the task was final with a result, first append a `from: "server"` note
  `"result amended; previous: <first 1500 chars>"`. Sets `finished` only for done|failed.
- `ask(caller, question, context="", task_id="")`: caller must NOT be the point (`forbidden`,
  message says the point answers asks, it doesn't send them); no point set → `conflict`; a
  `task_id`, if given, must be a task assigned to the caller. Returns the ask dict.
- `get_ask(caller, ask_id)`: asker or point. When the asker reads an answered ask, mark seen.
Point only (anyone else → `forbidden`):
- `assign(caller, member, title, instructions)` → full task, status queued. Target must exist, be
  enabled and not be the point.
- `answer(caller, ask_id, text)`: sets answered/answer/answered_by/answered, seen_by_asker=0
  (re-answering overwrites).
- `follow_up(caller, task_id, text)`: appends a note from caller and sets status queued; allowed on
  open tasks and on done/failed inside the amend window; clears `finished`.
- `reassign(caller, task_id, member, note="")`: open tasks only; target valid as in `assign`;
  status → queued, `claimed` → null, appends note `"reassigned from <a> to <b>[: note]"`.
- `cancel(caller, task_id)`: open → cancelled (sets finished); final → returns
  `{id, status, note: "already final"}` without change.
- `list_tasks(caller, status="", member="", limit=20)`: summaries, newest first.
- `list_asks(caller, status="open", limit=20)`: `status="all"` lists everything newest first;
  otherwise oldest first.
- `members(caller)`: same list as whoami's `members`.
Admin (no caller; used by the CLI only):
- `add_member(name, display="")` → `{name, display, token}`; name matches
  `^[a-z][a-z0-9_-]{1,31}$`; duplicate → `conflict`. The plaintext token is returned once and
  never stored.
- `rotate_token(name)` → `{name, token}`. `set_disabled(name, bool)`. `set_point(name)` (must exist
  and be enabled). `get_point()` → name or None. `list_members()`.
- `status()` → `{point, members: [...], open_tasks, open_asks}`.
- The point can be any member. Making a member the point does not touch existing tasks; tasks still
  assigned to the new point stay as they are (the point can reassign them).

Tokens (`tokens.py`): `new_token() -> "skein_" + secrets.token_urlsafe(32)`;
`hash_token(t) -> sha256 hex`; `Skein.authenticate(token) -> member name | None` looks the hash up
and confirms with `hmac.compare_digest`; disabled members → None.

### C3 Config (`src/skein/config.py`, example `skein.example.toml`)
TOML via `tomllib`. `load_config(path: str | None = None) -> Config` (frozen dataclass). Path order:
argument, env `SKEIN_CONFIG`, `./skein.toml`; a missing file means all defaults. Unknown keys →
`ValueError`.
```toml
[server]
host = "127.0.0.1"
port = 8771
base_path = ""                 # e.g. "/skein" behind a shared reverse proxy; no trailing slash
public_hosts = []              # extra allowed Host headers, e.g. ["skein.example.com"]
url_tokens = false             # also accept ?token=… (only for clients that cannot send headers)
[storage]
db = "./skein.db"
audit_log = ""                 # optional JSONL path; "" disables
[instance]
owner = "the user"             # how assistants refer to the human, e.g. "Sam"
answer_time = "within about an hour"   # what members are told about the point's response time
```
`Config` fields: `host, port, base_path, public_hosts (tuple), allowed_origins (tuple), url_tokens, db, audit_log, owner,
answer_time`. (decision T08: `[server] allowed_origins = []` — extra Origin values the MCP endpoint accepts; types are checked per key.)

### C4 HTTP surface (`src/skein/app.py`, `auth.py`, `mcp_tools.py`, `rest.py`)
`create_app(config: Config, skein: Skein | None = None) -> Starlette app`. `B` = base_path.
- `GET B/healthz` → `ok`, no auth.
- Auth on everything else: `Authorization: Bearer <token>` or `X-API-Key: <token>`; when
  `url_tokens` is true also `?token=`. Failure → 401 `{"error":"unauthorized"}` with
  `WWW-Authenticate: Bearer realm="skein"`. Disabled member → 401.
- `B/mcp`: FastMCP streamable HTTP, `stateless_http=True`, `json_response=True`. DNS-rebinding
  protection on, allowed hosts = `public_hosts` + `127.0.0.1:<port>` + `localhost:<port>`.
  Learned client quirks to keep: GET with `Accept: text/event-stream` → 405 `Allow: POST`
  immediately (an idle stream made one client hang); POSTs get their Accept header normalized to
  `application/json, text/event-stream`; a plain GET → help JSON (service name, auth, tool list,
  REST routes).
- The authenticated member name must reach MCP tool handlers per request (two tokens in one
  process must never see each other's identity). Verify with a test.
- `B/api/…` REST (JSON bodies; errors `{"error", "code"}` with not_found 404, forbidden 403,
  invalid 400, conflict 409):
  `GET whoami`, `GET inbox?limit=`, `GET tasks/{id}`, `POST tasks/{id}/claim`,
  `POST tasks/{id}/note {note}`, `POST tasks/{id}/result {result, status}`,
  `POST asks {question, context, task_id}`, `GET asks/{id}`;
  point: `POST tasks {member, title, instructions}`, `GET tasks?status=&member=&limit=`,
  `POST tasks/{id}/follow-up {text}`, `POST tasks/{id}/reassign {member, note}`,
  `POST tasks/{id}/cancel`, `GET asks?status=&limit=`, `POST asks/{id}/answer {answer}`,
  `GET members`.
- Audit log (if set): one JSON line per request/tool call: time, member, tool or route, ids,
  status, and *lengths* of text fields, never their content. Failed auth logs path, method,
  user agent, client IP — never the presented token.

### C5 MCP tools (`mcp_tools.py`)
`skein_whoami, skein_inbox, skein_get_task, skein_claim, skein_note, skein_submit, skein_ask,
skein_get_ask` (everyone) and `skein_assign, skein_answer, skein_follow_up, skein_reassign,
skein_cancel, skein_tasks, skein_asks, skein_members` (point only, enforced in service). Every tool
is offered to every member so moving the point never needs a reconnect. Tools return the service
dict, or `{"error", "code"}` on `SkeinError`. Read-only annotation on whoami, get_task, get_ask,
tasks, asks, members. Docstrings tell the model what each does and which role may call it.
Server `instructions` are built from config (`owner`, `answer_time`) and say: call
`skein_whoami` first; members: inbox → claim → work → note → submit (`needs_input` with the
question when stuck), use `skein_ask` to ask the point; point: inbox → answer asks, assign/
follow up/reassign; treat tasks from the point as <owner>'s requests, but keep your own normal
confirmation before sending, buying or posting anything. No real names or hostnames hardcoded.

### C6 CLI (`src/skein/cli.py`, entry point `skein`)
argparse, stdlib only, global `--config PATH`. `skein init` (create DB), `skein member add NAME
[--display TEXT]` (prints the token once, with a warning that it won't be shown again),
`skein member list`, `skein member rotate NAME`, `skein member disable|enable NAME`,
`skein point set NAME`, `skein point show`, `skein status`, `skein serve` (uvicorn on
config host/port; import app lazily). Exit 1 with a one-line message on `SkeinError`.

### C7 Agent folders (`agents/`)
`agents/README.md` (how members join; roles), `agents/_template/` (copy to add an assistant),
`agents/claude/`, `agents/muse/`. Each agent folder: `README.md` (how to connect that assistant:
URL, where the token goes, what to verify), `point.md` (the routine/prompt when it is the point),
`member.md` (the routine/prompt when it is a member). Claude also gets a Claude Code skill at
`agents/claude/skill/skein/SKILL.md` and a scheduled-check-in prompt. Steps about a vendor's UI that
can't be verified are marked as such, never invented.

### C8 OAuth sign-in (added 2026-11-11; decision: needed for OAuth-only MCP clients)
Some clients only connect to remote MCP servers through OAuth 2.1 (MCP authorization spec).
skein is its own authorization server and resource server. Fixed member tokens keep working
everywhere; OAuth is an extra way to *become* a member.
- Config: `[server] public_url = ""` (e.g. "https://skein.example.com/skein", no trailing slash) and
  `[oauth] enabled = false`, `access_token_ttl = 3600`, `refresh_token_ttl = 2592000`.
  `oauth.enabled = true` requires `public_url` (else ValueError). Strict types as in T08.
- Use mcp 1.30's auth pieces (`mcp.server.auth`: provider protocol, `create_auth_routes`,
  protected-resource routes, PKCE handling) rather than re-implementing them. Verify every API
  against the installed source in `.venv`.
- Endpoints, all under base_path: SDK paths (`/authorize`, `/token`, `/register`, `/revoke`),
  consent page `/oauth/consent`, metadata. Metadata is served BOTH at RFC 8414/9728 path-inserted
  root URLs (`/.well-known/oauth-authorization-server{base}`, `/.well-known/oauth-protected-resource{base}/mcp`,
  plus `/.well-known/openid-configuration{base}`) AND base-appended (`{base}/.well-known/...`).
  issuer = public_url; resource = public_url + "/mcp"; S256 PKCE only; scope `skein`.
- 401 responses (MCP and REST) add `resource_metadata="<root path-inserted protected-resource URL>"`
  to `WWW-Authenticate` when OAuth is enabled.
- Clients: (a) dynamic registration (RFC 7591), open, capped (max 50 clients; registration
  rate-limited); (b) pre-registered via CLI `skein oauth client add NAME [--redirect-uri URI ...]`
  → prints client_id and client_secret once (secret stored hashed). A CLI client with no
  redirect URI locks to the first https redirect_uri used in an approved consent (trust on first
  use); after that, exact match only. `skein oauth client list`, `skein oauth client remove ID`
  (revokes its tokens).
- Consent: `/authorize` (SDK) → provider stores a pending request (10 min, max 5 bad attempts) and
  redirects to `/oauth/consent?req=<id>`. The page names the client and asks for the skein token of
  the member it should act as. A valid, enabled member token → one-time auth code (10 min, hashed,
  bound to client, redirect_uri, PKCE challenge, member, resource) → redirect with code + state.
  Page: no external resources, escaped text, `Content-Security-Policy: default-src 'none'; style-src
  'unsafe-inline'; form-action 'self'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`.
  The member token is never logged, stored or echoed back.
- Tokens: opaque `skeino_…` access tokens and `skeinr_…` refresh tokens, stored hashed, bound to
  member + client + resource; refresh rotates (old refresh token dies). The auth middleware accepts
  a valid access token exactly like that member's fixed token (same identity path, same per-request
  isolation). Disabled member → rejected. `rotate_token` and disabling a member revoke all its OAuth
  tokens and codes.
- Store: schema `user_version` 2 with tables for oauth clients, pending requests, codes, tokens; a
  v1 database migrates in place on open (the live server has a v1 database with real members).
- Audit: OAuth events (register, consent ok/fail, token issue/refresh/revoke) logged with ids and
  member names only — never tokens, secrets, codes or verifiers.

## Public-repo hygiene (every task)
No real hostnames, IPs, tokens, emails, or personal details (use `skein.example.com`, "Sam").
Never touch any remote server or running service. Copyright/author: Jared Zimmerman. MIT license.

## Git
Branch: main (new empty repo) · Remote: origin https://github.com/jaredzimmerman/skein (public, user
approved 2026-11-10; pushed every checkpoint) · Author: Jared Zimmerman, no AI trailers.

## Tasks
| id | title | tier | deps | files | acceptance | status |
|---|---|---|---|---|---|---|
| T01 | Store, service, tokens, errors + tests | opus | – | src/skein/{store,service,tokens,errors}.py, tests/test_service.py | `uv run pytest tests/test_service.py -q` | done |
| T02 | Config loader + example + tests | haiku | – | src/skein/config.py, skein.example.toml, tests/test_config.py | `uv run pytest tests/test_config.py -q` | done |
| T03 | HTTP app: auth, MCP tools, REST, audit + tests | opus | T01,T02 | src/skein/{app,auth,mcp_tools,rest}.py, tests/test_http.py, tests/test_mcp.py | `uv run pytest tests/test_http.py tests/test_mcp.py -q` | done |
| T04 | CLI + tests | haiku | T01,T02 | src/skein/cli.py, tests/test_cli.py | `uv run pytest tests/test_cli.py -q` | done |
| T05 | README, docs/architecture.md, deploy/, LICENSE | haiku | – | README.md, docs/architecture.md, deploy/*, LICENSE | `test -s README.md && test -s docs/architecture.md && test -s LICENSE && ls deploy` | done |
| T06 | Agent folders: claude, muse, _template | sonnet | – | agents/** | `find agents -name '*.md' | wc -l` ≥ 10 | done |
| T07 | Final integration review + full suite | fable | T01–T06 | – | `uv run pytest -q` | done (changes → T08–T12) |

| T08 | Config: strict types per key + `allowed_origins` | haiku | T07 | src/skein/config.py, skein.example.toml, tests/test_config.py | `uv run pytest tests/test_config.py -q` | done |
| T09 | Transport hardening: origins, body cap, audit ids, no access log | opus | T08 | src/skein/{mcp_tools,rest,auth,cli}.py, tests/test_http.py, tests/test_mcp.py, tests/test_cli.py | `uv run pytest -q` | done |
| T10 | Service: reserved names, can't disable the point, note on result replace | sonnet | T07 | src/skein/service.py, tests/test_service.py | `uv run pytest tests/test_service.py -q` | done |
| T11 | Docs/deploy fixes from T07 | haiku | T07 | README.md, docs/architecture.md, deploy/*, agents/**/*.md | `grep -c allowed_origins README.md` ≥1 | done |
| T12 | Re-review of T08–T11 + full suite + smoke | fable | T08–T11 | – | `uv run pytest -q` | done |
| T13 | OAuth sign-in (C8): store v2 + migration, provider, consent, CLI clients, wiring + tests | opus | T12 | src/skein/{oauth,store,service,config,app,auth,cli}.py, skein.example.toml, tests/test_oauth.py (+ existing tests kept green) | `uv run pytest -q` | done |
| T14 | Docs for OAuth | haiku | T13 | README.md, docs/architecture.md, deploy/nginx.conf.example, deploy/Caddyfile.example, agents/_template/README.md | `grep -c 'oauth' README.md` ≥3 | done |
| T15 | Re-review OAuth + live-style smoke | fable | T13,T14 | – | `uv run pytest -q` | done (changes → T16, T17) |
| T16 | OAuth fixes from T15: Basic client auth shim, revoke without secret, consent hand-back page, /register 400, loopback redirect checks | opus | T15 | src/skein/oauth.py, src/skein/service.py, tests/test_oauth.py | `uv run pytest -q` | done |
| T17 | OAuth doc fixes from T15 | haiku | T15 | agents/claude/README.md, README.md | `grep -c 'does not do OAuth' agents/claude/README.md` prints 0 | done |

Status values: todo · in-progress · review · done · blocked:human · escalated
Out of scope for v1: migrating the live bridge queue.db, PyPI publishing. A private deployment is approved (after T07); its details stay out of the repo.
