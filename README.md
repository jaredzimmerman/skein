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
the point and a `member.md` for when it is a member. A client that can only sign in with OAuth needs
`[oauth]` turned on first; see OAuth sign-in below.

## OAuth sign-in

Some MCP clients connect only through OAuth and cannot send a bearer token. With OAuth on, skein
acts as its own OAuth server. The client starts a sign-in, the person approves it on skein's consent
page with a member's token, and the client gets its own tokens that act as that member. Fixed member
tokens keep working everywhere. OAuth is off by default, so turn it on only for a client that needs it.

### Config

In `skein.toml`:

```toml
[server]
public_url = "https://skein.example.com"   # no trailing slash; must end with base_path

[oauth]
enabled = true
access_token_ttl = 3600        # seconds
refresh_token_ttl = 2592000    # seconds (30 days)
```

`oauth.enabled = true` needs `server.public_url`, the address clients use: scheme and host, then
exactly `base_path`. With a path prefix, `public_url` ends with it, for example
`https://skein.example.com/skein`. It must use https (plain http is accepted only for localhost), and
it must not have credentials, a query or a fragment. A missing or wrong value stops the server at
startup with an error that names the key. `enabled` must be `true` or `false`.

With OAuth on, these routes sit under `base_path` and need no token:

- `/authorize`, `/token`, `/register` and `/revoke`, the standard OAuth endpoints.
- `/oauth/consent`, the sign-in page.
- Metadata at the root, with the base path appended: `/.well-known/oauth-authorization-server<base_path>`,
  `/.well-known/openid-configuration<base_path>` and `/.well-known/oauth-protected-resource<base_path>/mcp`.
  The same metadata is also served under `<base_path>/.well-known/`.

A request to `/mcp` or `/api/` without a valid token gets a 401 whose `WWW-Authenticate` header names
the protected-resource metadata, so a client can find the sign-in on its own. The reverse proxy must
forward the root `/.well-known/` paths. Both examples in `deploy/` do.

### How a sign-in works

1. The client sends the person to `/authorize` with PKCE (S256 only) and scope `skein`.
2. skein keeps the request for 10 minutes and shows the consent page. The page names the client and
   asks for the skein token of the member the client should act as.
3. The person pastes that member's token. skein checks it once and does not keep it, log it or echo
   it back. A valid token of an enabled member sends the person to the client's redirect address with
   a one-time code, valid for 10 minutes. A wrong token counts as a failed attempt; after five, that
   sign-in stops and the client must start again. The consent page is sent with `Cache-Control:
   no-store` and cannot be framed.
4. The client exchanges the code at `/token` for an access token (`skeino_…`, one hour by default) and
   a refresh token (`skeinr_…`, 30 days by default). Each refresh issues a new refresh token, and the
   old one stops working.

An access token acts exactly like that member's fixed token: the same member, the same permissions and
the same per-request separation from other members. Each token is bound to its member, its client and
the server's resource, and only a hash is stored.

### Clients

A client gets a client ID in one of two ways.

- **Dynamic registration.** The client registers itself at `/register` (RFC 7591), with no setup.
  Its redirect addresses may be https, http on localhost, or a private-use app scheme (for example
  `com.example.app:/callback`). At most 50 clients can register this way. At the cap, registrations
  more than a day old that hold no code or token (idle registrations) are removed to make room. Registration is
  limited to 10 per minute for the whole server; a client over the limit gets 429 with `Retry-After`.
- **Pre-registered.** The owner adds the client on the server:

  ```bash
  uv run skein oauth client add NAME --redirect-uri https://client.example.com/callback
  ```

  The command prints the `client_id` and `client_secret` once. skein keeps only a hash of the secret,
  and the client sends it at `/token` or `/revoke`, by basic or post authentication. Use this when a
  client needs a fixed ID and secret, or when you want the redirect addresses fixed in advance.

Redirect addresses are matched exactly; loopback http addresses match any port.

A pre-registered client added without `--redirect-uri` has no redirect address yet. Its first https
redirect address, used in an approved sign-in, becomes its only one (trust on first use). Until that
first approved sign-in, skein sends error replies to any https address the request names, so pass
`--redirect-uri` whenever you know it.

Other client commands:

```bash
uv run skein oauth client list
uv run skein oauth client remove CLIENT_ID
```

`client list` shows each client's ID, name, how it was added, its sign-ins and its redirect addresses.
`client remove` also revokes every token that client holds.

### What ends access

- `uv run skein member rotate NAME` ends every OAuth grant and unused code for that member, and the
  old fixed token stops working at once.
- `uv run skein member disable NAME` ends the same grants and refuses the member until it is enabled
  again. Enabling it does not restore them; the app has to sign in again.
- `uv run skein oauth client remove CLIENT_ID` ends every token that client holds, for any member.
- A client can end its own grant by calling `/revoke` (RFC 7009). That ends the access and refresh
  tokens together. A refresh token that has already been rotated is refused, but that alone does not
  end the grant.
- Using an authorization code a second time ends the grant that code produced.
- Setting `[oauth] enabled = false` refuses existing OAuth access tokens. Fixed member tokens keep
  working.

When `storage.audit_log` is set, OAuth events are written too: client registration, each consent
outcome, token issue, refresh and revoke, a reused code, and a refused registration. They hold ids,
member names, attempt counts and status, and never tokens, client secrets, codes or verifiers.

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
  member is added or rotated. Disabled members are refused even with a valid token. OAuth access and
  refresh tokens and OAuth client secrets are stored as hashes in the same way (see OAuth sign-in).
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
match your hostname and path. With OAuth on, set `public_url` to match too, and make sure the proxy
forwards the root `/.well-known/` metadata paths (both examples do). If you use `url_tokens`, keep
query strings out of the proxy's access log. `docs/architecture.md` describes how the parts fit together.

## License

MIT. See [LICENSE](LICENSE).
