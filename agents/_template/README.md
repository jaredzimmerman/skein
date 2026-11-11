# <Assistant name>

Copy this folder to `agents/<name>/` and fill in the placeholders.

## Connect

- Connector type: <custom connector / MCP config / REST only>. Source: <link to the vendor docs you checked>.
- URL: `https://skein.example.com/mcp` (REST: `https://skein.example.com/api/...`).
- Auth: `Authorization: Bearer <token>` (or `X-API-Key`). If the assistant cannot send headers, `url_tokens = true` allows `?token=` but the token then lands in URLs and logs; rotate it often. Turn the reverse proxy's access log off for skein's location, or strip query strings from it, before you use this.
- OAuth-only connectors: if the assistant's connector only accepts OAuth, enable `[oauth]` in `skein.toml` (with `server.public_url`), use the URL above, and approve the sign-in with this member's token on skein's consent page. See the README's OAuth sign-in section. Fixed tokens are not needed for that connector.
- Steps in the vendor UI: <write only what you verified; mark the rest "unverified: check X">.
- Local-only (stdio) MCP apps: if the assistant can only start a local MCP command, bridge it to skein with a stdio-to-HTTP proxy such as `mcp-remote` (npm), pinned to a release you've checked, for example `npx -y mcp-remote@<version> https://skein.example.com/mcp --header "X-API-Key:<token>"`. Wrap it in a small script that sets `PATH` and reads the token from a file only you can read, because desktop apps start commands with a minimal environment. `X-API-Key` avoids the space in `Bearer <token>`, which some command fields split.

## Schedule

<How this assistant runs a check-in on a schedule, its shortest interval and any limit on recurring jobs, or "on request only". See "Making a member work unattended" in agents/README.md.>

## Checklist

- [ ] `skein member add <name>`; token stored in the connector only
- [ ] Connector type chosen and added
- [ ] Auth header or token set
- [ ] Schedule or trigger chosen
- [ ] Test: ask the assistant to call `skein_whoami`; the name and role are right
- [ ] Test: as point, `skein_members` lists the member; as member, `skein_inbox` works
- [ ] Test: a task queued between scheduled runs is claimed and submitted by a later run, with no chat open
- [ ] `point.md` and `member.md` pasted into the schedule or instructions
