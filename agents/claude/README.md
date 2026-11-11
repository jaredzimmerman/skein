# Claude

Claude can be the point or a member. Create its member first: `skein member add claude` (the token prints once). Server URL below is `https://skein.example.com`.

Verification status: commands and UI steps were checked against Anthropic's docs on 2026-11-10 (code.claude.com/docs/en/mcp, support.claude.com custom connectors article). Re-check if the UI looks different.

## Claude Code

```bash
claude mcp add --transport http skein https://skein.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

- `--scope user` makes it available in all projects; the default is local (this project, private). Use project scope only if you do not commit the token: `.mcp.json` can reference `${SKEIN_TOKEN}` instead of the literal value.
- Verified in docs: `claude mcp add` does not validate the token. Run `/mcp` to see whether the server connected.
- Verified in docs: with an `Authorization` header set, Claude Code does not fall back to OAuth (skein uses bearer tokens, so that is what you want).
- Install the skill: copy `agents/claude/skill/skein/` to `~/.claude/skills/skein/`. Then say "check skein".
- Test: ask "call skein_whoami".

## claude.ai and Claude desktop (custom connectors)

Per the support article: available on Free (one custom connector), Pro, Max, Team, Enterprise. Claude connects from Anthropic's cloud, so the server must be reachable from the public internet (or allowlist Anthropic's IPs).

1. Customize > Connectors > "+ Add" > "Add custom connector".
2. Enter a name and `https://skein.example.com/mcp`, Continue.
3. Authentication: choose "No sign in" (skein does not do OAuth).
4. Under "Request headers", add `Authorization: Bearer <token>`. The article says this field takes fixed credentials such as bearer tokens, sent on every request, stored encrypted.
5. Add. Enable the connector per conversation with "+" > Connectors.

Notes: on Team/Enterprise one owner adds the connector and everyone shares the same credential, so use a dedicated skein member for it. To change the token you must remove and re-add the connector. The article gives no separate Claude desktop steps; assume the same flow and check.

Unverified: the exact label of the header field input (name and value boxes vs one line). Check on screen.

### Fallback if no header can be sent: `url_tokens`

If your plan or client has no request-headers field, set `url_tokens = true` in the skein config and use `https://skein.example.com/mcp?token=<token>` as the connector URL. Risk: the token sits in the URL, so it can appear in proxy and server logs, browser history and screenshots. skein's audit log never records it, but your reverse proxy may. Use a dedicated member, rotate with `skein member rotate claude`, and turn `url_tokens` off once headers work.

## Running a check-in on a schedule

See `scheduled-checkin.md` (cloud, desktop, or `/loop`). Claude acts only when run, so an unscheduled Claude sees new tasks only when someone says "check skein".

## Routines

`point.md` when Claude is the point, `member.md` when it is a member.
