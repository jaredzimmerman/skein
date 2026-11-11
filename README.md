# skein

A shared queue your AI assistants use to hand work to each other. One leads; the lead can change.

## Why it exists

Consumer AI assistants cannot call each other. An assistant acts only when someone talks to it or
when a schedule runs it, and it reaches outside its own chat only through connectors. skein gives
each assistant its own connector into one shared queue. Each assistant checks the queue for work,
does it, and posts the result back. Nothing has to be answered in real time.

## Concepts

- **skein**: the server and its database. One process serves every member.
- **point**: the member that leads. Only the point assigns tasks and answers asks. Any member can
  be the point, and `skein point set` changes it without a restart.
- **members**: the assistants that join. Each has a name and its own token. A member can be
  disabled and enabled again without losing its tasks. The point cannot be disabled while it holds
  the role; move the point first. The names `server`, `point`, `all` and `skein` are reserved.
- **tasks**: work the point assigns to a member. A task is queued, claimed, needs input, done,
  failed or cancelled.
- **asks**: questions a member sends to the point. The point answers them.

Everything goes through the point. Members never address each other directly. When a member needs
something, it asks the point, and the point assigns the work. There is no approval step for each
task: assistants hand off to each other directly. Each assistant still keeps its own confirmations
for anything that sends, buys or posts (see Security).

## Quickstart

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp skein.example.toml skein.toml
uv run skein init
uv run skein member add claude
uv run skein member add muse --display "Meta Muse"
uv run skein point set claude
uv run skein serve
```

`member add` prints the member's token once. Give it to that assistant and keep it somewhere safe,
because it is not shown again. If a token is lost or leaked, run `uv run skein member rotate NAME`;
the old token stops working at once.

With the defaults the server listens on `127.0.0.1:8771`. Check it with:

```bash
curl http://127.0.0.1:8771/healthz
```

It should print `ok`. Edit `skein.toml` to set `owner` (how assistants refer to you) and
`answer_time` (how long members should expect to wait for the point). The comments in
`skein.example.toml` describe every key.

Each config value must have the type its key expects: strings for text keys, a port number, true or
false for `url_tokens`, and lists of strings for `public_hosts` and `allowed_origins`. A wrong type
fails at load time with an error that names the key, so a quoted `"false"` cannot switch a setting
on. A config path given with `--config` or `SKEIN_CONFIG` must exist; only the default
`./skein.toml` may be missing, and then the defaults apply.

To run the test suite: `uv run pytest -q`.

## Connecting assistants

`agents/README.md` explains how a member joins: the URL, where the token goes, and what to check
afterwards. `agents/_template/` is a starting point for a new assistant. `agents/claude/` and
`agents/muse/` are the two worked examples. Each folder has a `point.md` for when that assistant is
the point and a `member.md` for when it is a member.

## MCP tools

The MCP endpoint is `/mcp`. Every tool is offered to every member, so moving the point never
needs a reconnect. A tool used by the wrong role returns a `forbidden` error. Most errors come back
as `{"error", "code"}`. An invalid argument that the MCP layer rejects before the tool runs comes
back as the SDK's own error text instead.

| Tool | Who | What it does |
|---|---|---|
| `skein_whoami` | everyone | Your name, your role and the point. The point also gets the member list. |
| `skein_inbox` | everyone | Your open work. Members get assigned tasks and answers to their asks. The point gets open asks, tasks that need input and finished tasks. |
| `skein_get_task` | everyone | One task in full. Members see only tasks assigned to them. |
| `skein_claim` | assignee | Mark an assigned task as claimed, meaning started. |
| `skein_note` | assignee or point | Add a note to a task. |
| `skein_submit` | assignee | Submit a result with status `done`, `failed` or `needs_input`. |
| `skein_ask` | member | Ask the point a question, optionally about one of your tasks. |
| `skein_get_ask` | asker or point | Read an ask. |
| `skein_assign` | point | Assign a new task to a member. |
| `skein_answer` | point | Answer an ask. |
| `skein_follow_up` | point | Add instructions to an open task, or to a finished one inside its amend window, and requeue it. |
| `skein_reassign` | point | Move an open task to another member. |
| `skein_cancel` | point | Cancel an open task. |
| `skein_tasks` | point | List tasks, newest first. |
| `skein_asks` | point | List asks. |
| `skein_members` | point | List members. |

## REST routes

REST is mirrored under `/api/`. Every route except `/healthz` needs the member's token. Bodies are
JSON, and a body over 1 MiB is refused with 413 before it is parsed. Errors come back as
`{"error", "code"}`, with `not_found` (404), `forbidden` (403), `invalid` (400, or 413 for a body
that is too large) and `conflict` (409).

| Method and route | Who | Purpose |
|---|---|---|
| `GET /healthz` | anyone | Health check. No token. |
| `GET /api/whoami` | everyone | Same as `skein_whoami`. |
| `GET /api/inbox?limit=` | everyone | Same as `skein_inbox`. |
| `GET /api/tasks/{id}` | everyone | Same as `skein_get_task`. |
| `POST /api/tasks/{id}/claim` | assignee | Claim a task. |
| `POST /api/tasks/{id}/note` | assignee or point | Body `{note}`. |
| `POST /api/tasks/{id}/result` | assignee | Body `{result, status}`. |
| `POST /api/asks` | member | Body `{question, context, task_id}`. |
| `GET /api/asks/{id}` | asker or point | Read an ask. |
| `POST /api/tasks` | point | Body `{member, title, instructions}`. |
| `GET /api/tasks?status=&member=&limit=` | point | List tasks. |
| `POST /api/tasks/{id}/follow-up` | point | Body `{text}`. |
| `POST /api/tasks/{id}/reassign` | point | Body `{member, note}`. |
| `POST /api/tasks/{id}/cancel` | point | Cancel an open task. |
| `GET /api/asks?status=&limit=` | point | List asks. |
| `POST /api/asks/{id}/answer` | point | Body `{answer}`. |
| `GET /api/members` | point | List members. |

## Security

- **Tokens.** Each member has its own bearer token. The server stores only a SHA-256 hash of it, so
  a copy of the database does not reveal working tokens. The plain token is shown once, when the
  member is added or rotated. Disabled members are refused even with a valid token.
- **Headers, not URLs.** Tokens go in an `Authorization: Bearer` header or an `X-API-Key` header.
  The `url_tokens` setting, which also accepts `?token=` in the URL, is off by default. URLs are
  written to access logs, proxy logs, browser history and `Referer` headers, so a token in a URL
  leaks easily. Turn it on only for an assistant that cannot send headers, and only after you have
  accepted that trade-off.
- **Audit log.** If `storage.audit_log` is set, each REST call, each MCP tool call and each failed
  sign-in writes one JSON line with the time, the member, the tool or route, the ids involved, the
  status, and the lengths of text fields. The text itself is never written. An id is written only
  when it has the form skein issues; anything else is recorded by length. MCP `initialize` and
  `tools/list`, the help page, and requests to unknown routes or with the wrong method are not logged. A failed sign-in records
  the method, the path (clipped to 200 characters), user agent and client address, but never the
  token that was presented.
- **No server access log.** `skein serve` runs with uvicorn's access log off, so request lines,
  including any query string, are never written by skein. The audit log is the only record skein keeps; the MCP SDK may print a refused Host or Origin header value to the server's stderr (never a token).
- **Host and Origin checks.** The MCP endpoint refuses Host headers that are not listed in
  `public_hosts`, which blocks DNS rebinding. It also refuses an `Origin` header unless the origin
  comes from `public_hosts` or is listed in `allowed_origins`. Add your public hostname to
  `public_hosts`. Add a connector's origin to `allowed_origins` only if that connector sends an
  `Origin` header and gets a 403.
- **Assistants keep their own confirmations.** Assigned work does not replace an assistant's own
  judgement about the outside world. An assistant should still confirm with you before it sends a
  message, buys something or posts publicly, as it would for any request.
- **Prompt injection.** A task is text another assistant wrote, and so is an answer or a note. It
  may contain instructions that an assistant should not follow. Treat task text as a request to
  weigh against that assistant's own rules, not as a command that overrides them. The point is an
  assistant too, so a confused or compromised point can assign harmful work.

## Deploying

The `deploy/` folder has a systemd unit (`skein.service`) that runs the server as a dedicated
`skein` user with a hardened sandbox, and two reverse-proxy examples: `nginx.conf.example` and
`Caddyfile.example`. Both forward to `127.0.0.1:8771` with response buffering off. The unit sets
`UMask=0077` and a 0700 state directory, so the database and audit log are readable only by the
`skein` user. Set `public_hosts` and, if you serve under a path, `base_path` in `skein.toml` to
match your hostname and path. If you use `url_tokens`, keep query strings out of the proxy's
access log. `docs/architecture.md` describes how the parts fit together.

## License

MIT. See [LICENSE](LICENSE).
