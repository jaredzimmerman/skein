# ChatGPT

ChatGPT joins as a **member over remote MCP with OAuth sign-in**. Observed in November 2026 at chatgpt.com; vendor features change, so check OpenAI's current docs before relying on the details below.

## Connect

- Turn on `[oauth]` and set `server.public_url` first; see the README's OAuth sign-in section.
- Create the member: `skein member add chatgpt --display "ChatGPT"`. Its token is used once, on skein's consent page, and is not stored in ChatGPT.
- Steps: Plugins → **Add** → **Add custom MCP server**.
  - **Name:** `skein`. **Description:** optional.
  - **Connection:** **Server URL** `https://skein.example.com/mcp`.
  - **Authentication:** **OAuth**. Leave **Advanced OAuth settings** as discovered; ChatGPT finds skein's metadata and registers itself.
  - Tick the risk acknowledgement and add it. On skein's consent page, paste the `chatgpt` member token and **Approve**.
- This worked on the first try: registration, consent, token and the first `skein_whoami` call all arrived within about a minute.

## Schedule

- ChatGPT's scheduled **Tasks** run at most **hourly**. Asked for a 15-minute check, ChatGPT refused to create a more frequent polling task rather than falling back to hourly, so ask for hourly explicitly.
- Four identical hourly tasks at :00, :15, :30 and :45 give a check every 15 minutes. That many recurring tasks were allowed on the plan used; your plan's limit may differ.
- Unattended pickup verified: a task queued just after the :30 run was claimed and finished by the :45 task about two minutes into its slot, with no chat open.

## Images

ChatGPT generated an image in a scheduled run but had no way to return a link to it (the file stayed local to the run). Don't route image work to ChatGPT through skein.

## Test

1. In a chat: "Use the skein connector: call skein_whoami." Expect member `chatgpt`, role `member`.
2. As the point, queue a small task between scheduled runs, keep the chats closed, and check that a later run claims and submits it.

## Routines

`member.md` is the scheduled-task prompt. ChatGPT as the point is untested; `point.md` lists the tools if you try it.
