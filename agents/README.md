# Agents

One folder per assistant. Each says how that assistant connects to a skein server and what to do on each check-in.

## Roles

- **Point**: the lead, chosen by the owner (`skein point set NAME`). Every task and every question goes through the point. It answers asks, assigns and reassigns tasks, follows up, and reviews finished work.
- **Member**: everyone else. A member checks its inbox, claims tasks, does the work, and submits a result. When it needs something it asks the point. Members never address each other.

Any member can be the point. Every tool is offered to every member, so moving the point never needs a reconnect.

## How a member joins

1. The owner runs `skein member add NAME`. The token is printed once; store it in the assistant's connector, never in a task.
2. Connect the assistant to `https://skein.example.com/mcp` (MCP, streamable HTTP) or the REST routes under `/api`, with `Authorization: Bearer <token>`.
3. Call `skein_whoami` and confirm the name and role.
4. Give the assistant its routine: `point.md` or `member.md` from its folder, on a schedule or when the user says "check skein".

Assistants only act when someone talks to them or on a schedule, so a check-in is a prompt run by one of those.

## Folders

- `_template/`: copy to add a new assistant.
- `claude/`: Claude (Claude Code, claude.ai, Claude desktop), plus a Claude Code skill.
- `muse/`: Meta Muse.

Tool names are fixed: `skein_whoami`, `skein_inbox`, `skein_get_task`, `skein_claim`, `skein_note`, `skein_submit`, `skein_ask`, `skein_get_ask` (everyone) and `skein_assign`, `skein_answer`, `skein_follow_up`, `skein_reassign`, `skein_cancel`, `skein_tasks`, `skein_asks`, `skein_members` (point only).
