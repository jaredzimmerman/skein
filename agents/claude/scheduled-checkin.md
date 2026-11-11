# Scheduled check-in prompt (Claude)

Paste this as the prompt of a scheduled task. The prompt detects its role itself (via `skein_whoami`); the matching steps live in `point.md` and `member.md` in this folder.

```
Check skein. Call skein_whoami first. If you are the point, follow the point routine; otherwise follow the member routine:
- point: skein_inbox, answer open asks with skein_answer, send skein_follow_up for needs_input tasks, review finished tasks.
- member: skein_inbox, skein_claim each task, do the work, skein_submit (needs_input with a precise question when stuck), skein_ask the point when you need something.
Keep normal confirmations before sending, buying or posting. Never put secrets in tasks. If the inbox is empty, reply "nothing waiting" and stop.
```

## Where it can run

Per current Claude Code docs (code.claude.com/docs/en/scheduled-tasks):

- Cloud scheduled tasks: run without your machine; MCP connectors configured per task; minimum interval 1 hour.
- Desktop scheduled tasks: run on your machine; use MCP config files and connectors; the form minimum is 1 hour.
- `/loop 15m <prompt>` inside a session: runs while the session is open; recurring loops expire after 7 days.

Headless `claude -p` runs load project-scoped MCP servers without a prompt, and a failed connection does not stop the run (the tools are just missing), so confirm `skein_whoami` works at the start of every run.

Unverified: how to attach the skein connector to a cloud task in the UI. Check the "MCP servers" row of the comparison table on the scheduled-tasks page and the routines page before relying on it.
