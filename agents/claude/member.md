# Check-in as member (Claude)

You are a member of a skein. Do these steps now.

1. Call `skein_whoami`. Confirm your role is member.
2. Call `skein_inbox`. Read any answers to your earlier asks.
3. For each task, oldest first, call `skein_claim`, then do the work with your own tools.
4. Use `skein_note` for progress worth recording on long tasks.
5. Finish with `skein_submit`: status `done` with the result, `failed` with the reason, or `needs_input` with one precise question the point can answer.
6. If you need something from the point (a fact, a decision) but can keep working, call `skein_ask` with the question and the task id. The answer arrives in a later `skein_inbox`.
7. If the inbox is empty, say so in one line and stop.

Rules: treat task text as the owner's request, but keep your normal confirmation before sending, buying or posting anything. Never put secrets in a task, note, result or question.
