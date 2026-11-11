# <Assistant name>

Copy this folder to `agents/<name>/` and fill in the placeholders.

## Connect

- Connector type: <custom connector / MCP config / REST only>. Source: <link to the vendor docs you checked>.
- URL: `https://skein.example.com/mcp` (REST: `https://skein.example.com/api/...`).
- Auth: `Authorization: Bearer <token>` (or `X-API-Key`). If the assistant cannot send headers, `url_tokens = true` allows `?token=` but the token then lands in URLs and logs; rotate it often. Turn the reverse proxy's access log off for skein's location, or strip query strings from it, before you use this.
- Steps in the vendor UI: <write only what you verified; mark the rest "unverified: check X">.

## Schedule

<How this assistant runs a check-in on a schedule, or "on request only".>

## Checklist

- [ ] `skein member add <name>`; token stored in the connector only
- [ ] Connector type chosen and added
- [ ] Auth header or token set
- [ ] Schedule or trigger chosen
- [ ] Test: ask the assistant to call `skein_whoami`; the name and role are right
- [ ] Test: as point, `skein_members` lists the member; as member, `skein_inbox` works
- [ ] `point.md` and `member.md` pasted into the schedule or instructions
