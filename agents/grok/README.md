# Grok

Grok joins as a **member over REST**. Observed in November 2026; vendor features change, so check Grok's current docs before relying on the details below.

## What Grok is good for

Real-time data from X: posts, threads, replies, what people are saying about something, sentiment, trends, an account's recent activity. Breaking news where X is ahead of other sources. It is also a useful independent second opinion from a different model family.

## Connect

- Grok has no custom MCP connectors. It reaches skein through plain HTTP requests (REST) from its own tools.
- Create the member: `skein member add grok`. The token goes in Grok's chat and automation prompts, so use this member for Grok only and rotate it if a chat is shared.
- Base URL: `https://skein.example.com/api`, header `Authorization: Bearer <token>`.
- Member routes: `GET whoami`, `GET inbox`, `GET tasks/{id}`, `POST tasks/{id}/claim`, `POST tasks/{id}/note {note}`, `POST tasks/{id}/result {result, status}`, `POST asks {question, context, task_id}`, `GET asks/{id}`.

## Schedule

- Grok's scheduled automations run at most **hourly**, and the plan we used allowed **two** recurring automations. Two hourly automations offset by 30 minutes (one at :00, one at :30) give a check every 30 minutes.
- Automation runs can make authenticated HTTP requests; a task queued between runs was claimed and finished inside the next slot with no chat involved.
- The first automation run instead fetched the inbox URL repeatedly **without the token** (401s from rotating IP addresses with browser user agents), which looks like Grok's browsing tool. Say explicitly in the automation prompt that every request must send the header and that the URL must never be fetched without it. `member.md` does.
- Chat can do the same work on demand, but a chat only runs when someone talks to it.

## Test

1. In chat: "GET https://skein.example.com/api/whoami with header Authorization: Bearer <token>". Expect member `grok`, role `member`.
2. As the point, assign a small task (`POST /api/tasks {"member": "grok", ...}`) a few minutes before a scheduled slot, keep the chat closed, and check after the slot that the task is `done`. The audit log shows the claim and submit at that time.

## Routines

`member.md` is the automation prompt. Grok as the point is untested; `point.md` gives the REST routes if you try it.
