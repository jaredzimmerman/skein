# Architecture

skein is one Python process that serves a shared queue to several AI assistants. This document
describes how the parts fit together. The exact contracts (data model, service methods, config
keys, routes and tools) are frozen in `.ladder/PLAN.md`; this document describes the same design
in prose.

## Overview

```
assistant ──(MCP streamable HTTP or REST, Bearer <member token>)──▶ app.py (Starlette)
   auth middleware: token → member name (disabled members rejected)
   ├─ /mcp       mcp_tools.py  (FastMCP, stateless, JSON responses)  ─┐
   └─ /api/...   rest.py                                              ├─▶ service.py (all rules and authorization) ─▶ store.py (SQLite)
                                                                      ─┘
CLI (cli.py) ──▶ service.py admin functions ──▶ store.py      (members, tokens, point; no network)
```

Two transports sit in front of one service. The MCP endpoint and the REST routes both call the same
functions in `service.py`. The CLI calls the admin functions in the same service and never opens
the network.

## Layers

- **`store.py`** reads and writes SQLite. It holds no rules. It uses WAL mode and one connection per
  operation, so it is safe across threads and processes.
- **`service.py`** is the single place where authorization, routing and state changes happen. Every
  member method takes the authenticated caller's name first and returns plain, JSON-safe dicts.
  Errors are `SkeinError` with a code of `not_found`, `forbidden`, `invalid` or `conflict`.
- **`tokens.py`** creates and hashes tokens. Tokens are `skein_` plus 32 random URL-safe bytes. Only
  the SHA-256 hash is stored, and a presented token is checked with a constant-time comparison.
- **`auth.py`** turns a presented token into a member name using `Skein.authenticate`.
- **`mcp_tools.py`** exposes the service as MCP tools. Each tool converts a `SkeinError` into an
  `{"error", "code"}` result instead of a transport error.
- **`rest.py`** exposes the same operations as JSON routes under `/api/`, with the same error codes
  mapped to HTTP status codes.
- **`app.py`** builds the Starlette application, mounts `/healthz`, `/mcp` and `/api/`, and applies
  authentication and the audit log to everything except `/healthz`.
- **`config.py`** loads the TOML config into a frozen dataclass. Unknown keys are an error, and each
  value is checked against the type its key expects.
- **`cli.py`** provides the `skein` command: setup, member and point management, status and serve.

Transports never query the store directly and never repeat a rule. A new rule goes in the service,
and both transports pick it up.

## Identity per request

The member name comes from the token on each request, and it is passed to the MCP tool handlers for
that request only. Two tokens served by the same process never see each other's identity. A test
checks this.

## Roles

There is one point at a time. The point is stored in the database as a meta value, so
`skein point set` takes effect without a restart. Every other enabled member is a member. The role
is decided per call in the service, so the same connector works in either role.

Making a member the point does not change existing tasks. Tasks still assigned to the new point
stay as they are, and the point can reassign them. The point cannot be disabled: `member disable`
on the current point is refused with a conflict, and the point must be moved first.

## Task and ask lifecycle

A task moves through these states:

```
queued ──claim──▶ claimed ──submit──▶ done | failed | needs_input
   ▲                                       │
   └──────── follow_up (point) ◀───────────┘   (needs_input → queued)

any open state (queued, claimed, needs_input) ──cancel (point)──▶ cancelled
```

Open means queued, claimed or needs_input. Final means done, failed or cancelled. A cancelled task
accepts nothing further. Setting a task to done, failed or needs_input marks it as not yet seen by
the point, so it shows up in the point's inbox.

A done or failed task stays open to amendment for seven days after it finishes. During that time a
member can add notes and submit a new result, and the point can send follow-ups. Whenever a submit
replaces a non-empty result, whatever the task's status, the server first adds a note from
`server` holding the first 1500 characters of the previous result. A replaced result therefore
never disappears without a trace in the task's notes.

An ask is open until the point answers it. A new answer replaces the old one. The asker is told
when an answer arrives and sees it once.

## Data model

SQLite holds three tables and a small meta table:

- `members`: name, display name, token hash, creation time, disabled flag and last seen time. The
  names `server`, `point`, `all` and `skein` are reserved and cannot be used for a member.
- `meta`: key and value pairs. The key `point` holds the point's member name.
- `tasks`: id, title, instructions, assignee, creator, status, timestamps, result, notes and the
  point's seen flag. Notes are kept as a JSON list, and only the last 50 are kept.
- `asks`: id, asker, question, context, related task, status, answer, who answered, and the asker's
  seen flag.

Times are stored as Unix floats and returned as ISO-8601 strings ending in `Z`. A missing time is
returned as `null`. The schema is versioned with `PRAGMA user_version`.

Length limits are enforced in the service. Some inputs are rejected when they are too long, and
others, such as titles, results, notes and context, are truncated. Each limit is listed in
`.ladder/PLAN.md`.

## Configuration

Config is read from TOML with `tomllib`. The path comes from the command line, then the
`SKEIN_CONFIG` environment variable, then `./skein.toml`. A path given on the command line or in
the environment must exist. Only the default `./skein.toml` may be missing, and then defaults
apply. Each key is checked after parsing: strings for text keys, an integer port from 1 to 65535
(a boolean is refused), a real boolean for `url_tokens`, and lists of strings for `public_hosts`
and `allowed_origins`. `base_path` must be empty or start with a single `/`. A wrong type raises
an error that names the key.

The keys are grouped as `[server]` (host, port, base path, extra allowed hosts, extra allowed
origins, URL tokens), `[storage]` (database path and audit log path) and `[instance]` (owner name
and expected answer time). `skein.example.toml` lists them with comments.

## HTTP surface

- `GET /healthz` returns `ok` without authentication.
- Everything else requires `Authorization: Bearer <token>` or `X-API-Key: <token>`. A `?token=`
  query parameter is accepted only when `url_tokens` is on. A failed check returns 401 with a
  `WWW-Authenticate: Bearer realm="skein"` header.
- `/mcp` runs FastMCP in stateless, JSON-response mode. The server answers with plain JSON and does
  not stream. DNS-rebinding protection is on: the allowed hosts are `public_hosts` plus the local
  loopback names for the configured port. An `Origin` header is allowed only when it matches
  `public_hosts` or `allowed_origins`. The endpoint handles two client quirks: a GET that asks for
  an event stream gets a 405 right away, and a plain GET returns a short help document.
- `/api/` mirrors the MCP tools as REST routes. The full list is in the README. A request body over
  1 MiB is refused with 413 and code `invalid` before it is parsed.
- Argument errors that FastMCP rejects before a tool runs come back as the SDK's error text, not as
  `{"error", "code"}`.
- `skein serve` runs uvicorn with its access log off. Request lines, and any token in a query
  string, are never written to the server's log.

## Audit log

When `storage.audit_log` is set, each REST call, each MCP tool call and each failed sign-in adds
one JSON line. The line holds the time, member, tool or route, relevant ids, status, and the length
of each text field. An id is written only when it has the form skein issues (task ids, ask ids and
member names); anything else in an id position is recorded by its length. The text itself is never
logged. MCP `initialize` and `tools/list`, the help page, and requests to unknown routes or with the wrong method are not
audited. A failed authentication logs the method, the path (clipped to 200 characters), user agent
and client address, and never the token.

## Running it

One process serves every member. Routine operation uses the CLI against the same database:

- `skein init` creates the database.
- `skein member add|list|rotate|disable|enable` manages members and their tokens.
- `skein point set|show` manages the point.
- `skein status` shows the point, the members, and the number of open tasks and asks.
- `skein serve` starts the server on the configured host and port. It writes no access log; the
  audit log is the only request record.

Because members and the point live in the database, these commands take effect without restarting
the server. For deployment, see `deploy/`.
