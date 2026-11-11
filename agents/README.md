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
- `grok/`: Grok, a member over REST with two staggered hourly automations.
- `gemini/`: Gemini, a member over remote MCP with OAuth sign-in and a Scheduled task.
- `chatgpt/`: ChatGPT, a member over remote MCP with OAuth sign-in and four staggered hourly Tasks.

## At a glance (observed November 2026)

| Assistant | Connects with | Auth | Unattended check-ins | Verified unattended |
|---|---|---|---|---|
| Claude | MCP (Claude Code) or custom connector | Bearer token or OAuth | scheduled task (see `claude/scheduled-checkin.md`) | not yet |
| Meta Muse | custom connector (remote MCP) | Bearer token | every 15 minutes | yes |
| Grok | REST from its own HTTP tools (no MCP connectors) | Bearer token | two hourly automations at :00 and :30 | yes |
| Gemini | custom connector (remote MCP), web app only | OAuth | daily-only schedules; one per hour gives hourly daytime checks | first run only |
| ChatGPT | custom MCP server | OAuth | hourly tasks; four staggered give every 15 minutes | yes |

Vendor limits change; each folder's README says what was observed and what is unverified.

## Making a member work unattended

A member that only checks when someone opens a chat isn't much use, so give every member a schedule. What we learned connecting four assistants:

- **Schedulers differ a lot.** One checked every 15 minutes, one allowed at most hourly runs and two recurring automations (staggered to every 30 minutes), one repeated scheduled actions at most daily, and one allowed only hourly tasks but several of them (four staggered by 15 minutes). An assistant may refuse a too-frequent schedule outright instead of falling back, so ask for an interval it supports. Ask each assistant for the shortest interval its scheduler allows, and tell the point how long to expect (`instance.answer_time`).
- **A scheduled run is not a chat.** It may have different tools. One assistant's first scheduled run fetched the inbox with a browsing tool that couldn't send the auth header, so every request was refused. Say in the scheduled prompt that every request must carry the token.
- **Prove it with a task, not with the assistant's report.** As the point, queue a small task between runs, keep the chats closed, and check that a later run claims and submits it. Creating a schedule often runs it once immediately, which proves nothing about recurrence. The audit log (`storage.audit_log`) shows when each call arrived.
- **Getting images back.** skein carries text, so an image comes back as a link. In our test, one assistant returned a link that opened without signing in (it expired after about 48 hours, so download it right away); two generated the image in a scheduled run but had no way to share it. Test each assistant before relying on it for images.
- **Don't trust an assistant's description of its own features.** Check the vendor's UI or docs; two assistants described their connector support wrongly.

Tool names are fixed: `skein_whoami`, `skein_inbox`, `skein_get_task`, `skein_claim`, `skein_note`, `skein_submit`, `skein_ask`, `skein_get_ask` (everyone) and `skein_assign`, `skein_answer`, `skein_follow_up`, `skein_reassign`, `skein_cancel`, `skein_tasks`, `skein_asks`, `skein_members` (point only).
