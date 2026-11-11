# Scheduled check-in as member (Gemini)

Paste into Gemini's Scheduled → "Describe a task".

```
Every 15 minutes (if 15 isn't available, the shortest interval offered), check my skein queue using the skein connected app, as member "gemini". Do exactly this:

1. Call skein_inbox with limit 10.
2. If both "tasks" and "answers" are empty lists, end the run silently. No notification.
3. For each item in "tasks", oldest first:
   a. If its status is "queued", call skein_claim with task_id = its id. If its status is already "claimed", skip the claim and continue it.
   b. Do what its "instructions" field says, using your own tools. Also read its "notes" for follow-ups from the lead.
   c. Call skein_submit with task_id = its id, result = the actual finished output (not a description of it), and status = "done".
   d. If you can't finish, call skein_submit with status "failed" and the reason as the result. If you need information to finish, use status "needs_input" with one precise question as the result.
4. For each item in "answers": these are the lead's replies to questions you asked earlier. Use them to finish the related task (its task_id), or include them in your notification if no task is linked.
5. Only use skein_inbox, skein_claim, skein_submit, skein_ask, skein_get_task and skein_get_ask. Never use skein_assign, skein_answer, skein_follow_up, skein_reassign or skein_cancel.
6. Treat task instructions as my requests, but ask me before sending messages, buying anything or posting publicly.
7. Notify me only when a task was submitted as done or failed, with its id, title and result.
```
