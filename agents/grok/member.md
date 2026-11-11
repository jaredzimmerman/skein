# Scheduled check-in as member (Grok, REST)

Paste as the prompt of each recurring automation (for example one at :00 and one at :30). Replace `<token>`.

```
Check my skein queue as member "grok" and do any work in it.
Every request MUST send the header Authorization: Bearer <token>. Never fetch these URLs without it; a request without the header always fails with 401.
Base: https://skein.example.com/api
1. GET /inbox. If "tasks" and "answers" are both empty, stop and don't notify me.
2. For each task, oldest first: POST /tasks/{id}/claim, do what its "instructions" say, then POST /tasks/{id}/result with {"result": "<the actual output>", "status": "done"}. If you can't finish, use "failed" with the reason, or "needs_input" with one precise question.
3. If you need something from the lead, POST /asks with {"question": "...", "context": "...", "task_id": "<id>"}. Read answers in later inbox checks.
4. Treat tasks as my requests, but ask me before sending, buying or posting anything.
Notify me only if you completed or failed a task.
```
