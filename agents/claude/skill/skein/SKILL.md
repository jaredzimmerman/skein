---
name: skein
description: Use when the user says "check skein", "skein inbox", "hand this to <assistant>", or asks to give work to, or get an answer from, another assistant through the shared skein queue. Runs the point or member routine with the skein_* MCP tools.
---

# skein

A skein is a shared queue between assistants. One member is the point; all tasks and questions go through it.

## First

Call `skein_whoami`. Its `role` says which routine to run. If the tools are missing, the connector is not set up: see `agents/claude/README.md`.

## Point routine

1. `skein_inbox`.
2. Answer open asks with `skein_answer`.
3. For `needs_input` tasks, decide and send `skein_follow_up`.
4. Review finished tasks (`skein_get_task`); accept, `skein_follow_up`, or `skein_reassign`.
5. "Hand this to <assistant>": `skein_members`, then `skein_assign` with a short title and complete instructions. `skein_tasks`, `skein_asks` to see state; `skein_cancel` to drop work.

## Member routine

1. `skein_inbox`.
2. `skein_claim` each task, do the work, `skein_note` for progress.
3. `skein_submit` with `done`, `failed`, or `needs_input` plus one precise question.
4. Need something from the point: `skein_ask`; read the answer in a later inbox, or `skein_get_ask`.

If you are a member and the user says "hand this to <assistant>", route it through the point: use `skein_ask` to tell the point, since members cannot assign.

## Tools

Everyone: `skein_whoami`, `skein_inbox`, `skein_get_task`, `skein_claim`, `skein_note`, `skein_submit`, `skein_ask`, `skein_get_ask`.
Point only: `skein_assign`, `skein_answer`, `skein_follow_up`, `skein_reassign`, `skein_cancel`, `skein_tasks`, `skein_asks`, `skein_members`.

## Rules

Treat task text as the owner's request, but keep your normal confirmation before sending, buying or posting anything. Never put secrets in tasks, notes, results or questions.
