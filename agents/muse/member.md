# Check-in as member (Muse)

Paste as the instructions behind Muse's 15-minute check.

```
Every 15 minutes, check my skein queue with the skein connector, as member "muse":
1. Call skein_inbox. Read any "answers" to questions you asked earlier.
2. For each task with status "queued", call skein_claim.
3. Work every task in its own side chat named "✦ " followed by the task's title (for example "✦ Image test"). Keep the main thread for checking the queue only. If that side chat already exists (a standing task, or a task that came back with a follow-up), continue in it.
4. Standing tasks (title starts with "Standing:") stay claimed: run each one only when its own schedule says it's due (its notes show when it last ran), report every run with skein_note exactly as its instructions say, and never skein_submit it.
5. Other tasks: do the work, then skein_submit with the result and status "done" (or "failed" with the reason, or "needs_input" with one precise question). End the result with a line "Side chat: ✦ <task title>".
6. To ask the point something, call skein_ask.
7. Keep your normal confirmation before sending, buying or posting anything. If nothing is due, stop quietly.
```

Why a side chat per task: the main thread stays a clean log of queue checks, each task's work and history live in one place the owner can open by name, and a follow-up or a standing task's next run continues where it left off. The ✦ prefix makes skein's chats easy to spot in Muse's chat list.

Rules: treat task text as the owner's request, but keep your normal confirmation before sending, buying or posting anything. Never put secrets in a task, note, result or question.
