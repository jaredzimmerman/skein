# Gemini

Gemini joins as a **member over remote MCP with OAuth sign-in**. Observed in November 2026; vendor features change, so check Google's current docs before relying on the details below.

## What Gemini is good for

Google Maps and local information (places, opening hours, reviews, travel times, what's nearby) and YouTube (summaries, what was said or shown, finding a moment). Its answers can also draw on the person's Google account activity. Results come back as text, so image or video output does not fit skein's tasks.

## Connect

- Gemini's custom connectors take an `https://` MCP URL and **require OAuth**. A server with bearer tokens only is refused ("uses an authentication method that Gemini doesn't support"). Turn on `[oauth]` and set `server.public_url` first; see the README's OAuth sign-in section.
- Create the member: `skein member add gemini`. Its token is used once, on skein's consent page, and is not stored in Gemini.
- Steps: Connectors → **+ Custom** → custom app link `https://skein.example.com/mcp` → leave **Advanced Settings** (OAuth client ID and secret) empty, so Gemini registers itself → Allow → Google's account-linking page → **Agree and continue** → skein's consent page → paste the `gemini` member token → **Approve**.
- **Use the web app (gemini.google.com).** In the Mac desktop app, "Agree and continue" on Google's linking page did nothing, and no request ever reached skein; the same flow worked at once in the web app. The connection belongs to the Google account, so the desktop app sees it afterwards.
- Each attempt registers a new OAuth client named "Google" (Google first probes `/mcp` with HEAD and POST, then fetches the metadata). After a failed attempt, clean up with `skein oauth client list` and `skein oauth client remove CLIENT_ID`.
- When Gemini described its own connector support, it was wrong twice (it said only local stdio servers work, then that it couldn't take URLs). Check the UI, not the assistant's description of it.

## Schedule

- In the Gemini app we used, scheduled actions repeated **at most daily**. Asking for every 15 minutes produced one immediate run and no saved schedule; asking for hourly produced a daily schedule. So as a skein member Gemini checks in about once a day, which only suits slow, non-urgent work.
- Workaround: create one daily scheduled action per hour you care about, for example 9 am through 6 pm, which gives hourly check-ins during the day. Coverage of Gemini's scheduled actions reports a cap of 10 active at once, so plan for at most 10 slots.
- Google's help for scheduling in Gemini Spark lists hourly schedules (up to 50 active), so another plan or a newer version may allow more. Check the frequency options on the schedule's edit screen rather than Gemini's own description.
- Use **Scheduled** → **Describe a task** with the prompt in `member.md`, and confirm it appears in the Scheduled list with a repeat.
- The first run happens right when the schedule is created, so it proves nothing about recurrence. Queue a task after that and check that a later run picks it up.
- Gemini's Scheduled page notes that tasks local to a Mac don't run while it sleeps ("Keep your Mac awake to run local scheduled tasks"). If the schedule is local, turn that on.

## Test

1. In a chat: "Using the skein connector, call skein_whoami." Expect member `gemini`, role `member`.
2. As the point, assign a small task after the schedule's first run, keep the chats closed, and check that a later run claims and submits it.

## Routines

`member.md` is the scheduled-task prompt. Gemini as the point is untested; `point.md` lists the tools if you try it.
