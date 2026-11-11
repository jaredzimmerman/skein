# Handoff: 2026-11-11
Phase: 4 (OAuth sign-in, C8)
In flight: oauth-fixes workflow wf_b55eccf9-38d (T16, T17)
Next action: launch OAuth workflow; after T15 approves, redeploy (gitignored notes) with oauth enabled + public_url, add root .well-known proxy locations, verify live, user retries Gemini
Blocked on human: none (history rewrite approved; do it after wave 3, then force-push once)
Workflow: see LEDGER for the latest runId (script paths are local-only)
Notes:
- Machine-, account- and host-specific notes live only in gitignored *.local.md files. Never put them in tracked files.
- Python via uv 3.11; mcp 1.30.0.
