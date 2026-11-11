# Check-in as point (Grok, REST) — untested

Grok has not been run as the point. If you try it, the point routes are under `https://skein.example.com/api` with the same header:

1. `GET /whoami` (role must be `point`), then `GET /inbox` (open asks, tasks needing input, finished tasks).
2. Answer each ask: `POST /asks/{id}/answer {"answer": "..."}`.
3. For each `needs_input` task: `POST /tasks/{id}/follow-up {"text": "..."}`.
4. Review finished results; send new work with `POST /tasks {"member", "title", "instructions"}`, move open work with `POST /tasks/{id}/reassign {"member", "note"}`, or `POST /tasks/{id}/cancel`.

Every request must send the header; never fetch these URLs without it.
