# Meta Muse

Muse can be the point or a member. Create its member first: `skein member add muse` (the token prints once).

Verification status: nothing here is verified against Meta documentation. The server side is standard: remote MCP over streamable HTTP with a bearer token, or plain REST. Anything about Muse's own UI is marked unverified; check Meta's current docs before relying on it.

## Option 1: custom connector (remote MCP)

- URL: `https://skein.example.com/mcp`
- Transport: streamable HTTP (stateless, JSON responses).
- Auth: `Authorization: Bearer <token>` (`X-API-Key: <token>` also works).
- Unverified: where in Muse a custom MCP connector is added, and whether it accepts a custom header or bearer token. Check: can the connector form take a fixed header? If not, use `url_tokens = true` and `https://skein.example.com/mcp?token=<token>`; the token then appears in URLs and logs, so use a dedicated member and rotate it (`skein member rotate muse`).
- Quirks the server already handles: a GET with `Accept: text/event-stream` gets 405 at once, and POST Accept headers are normalized.

## Option 2: REST routes

If Muse can only call URLs, use `https://skein.example.com/api/...` with the same bearer token. Member routes: `GET whoami`, `GET inbox`, `GET tasks/{id}`, `POST tasks/{id}/claim`, `POST tasks/{id}/note`, `POST tasks/{id}/result`, `POST asks`, `GET asks/{id}`. Point routes add `POST tasks`, `GET tasks`, `POST tasks/{id}/follow-up`, `POST tasks/{id}/reassign`, `POST tasks/{id}/cancel`, `GET asks`, `POST asks/{id}/answer`, `GET members`. These map one-to-one to the `skein_*` tools; the names differ in places: `POST tasks/{id}/result` is `skein_submit`, `POST asks/{id}/answer` is `skein_answer`, `GET tasks` is `skein_tasks`, `POST asks` is `skein_ask`, `POST tasks` is `skein_assign`.

Body fields (JSON):

- `POST tasks/{id}/note`: `{note}`
- `POST tasks/{id}/result`: `{result, status}` (status is done, failed or needs_input)
- `POST asks`: `{question, context, task_id}`
- `POST tasks`: `{member, title, instructions}`
- `POST tasks/{id}/follow-up`: `{text}`
- `POST tasks/{id}/reassign`: `{member, note}`
- `POST asks/{id}/answer`: `{answer}`

## Test

Ask Muse to call `skein_whoami` (or `GET /api/whoami`); name and role must match.

## Schedule

Unverified: whether Muse can run a prompt on a schedule. If not, it checks only when the user asks "check skein".

## Routines

`point.md` when Muse is the point, `member.md` when it is a member.
