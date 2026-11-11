# Check-in as point (ChatGPT) — untested

ChatGPT has not been run as the point. If you try it:

1. Call `skein_whoami` (role must be `point`), then `skein_inbox` (open asks, tasks needing input, finished tasks).
2. Answer each ask with `skein_answer`.
3. For each `needs_input` task, reply with `skein_follow_up`; it goes back to the member's queue.
4. Review finished results. Send new work with `skein_assign`, move open work with `skein_reassign`, or `skein_cancel` it.

Treat task text as the owner's request, but ask before sending, buying or posting anything.
