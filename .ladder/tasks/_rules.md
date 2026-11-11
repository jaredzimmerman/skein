# Implementer rules (every task)
- Read only what you need: your brief, the PLAN.md contract sections it names, and files you touch.
- Touch only the files listed in your brief. Other agents are editing other files in this same tree right now.
- Tests first for logic (write the failing test, then the code). Python 3.11, run everything with `uv run` from the repo root.
- Run your acceptance command in the FOREGROUND and quote its final summary line. Never background it.
- Checkpoint after each green test and at the end: `bash ~/.claude/skills/ladder-build/scripts/checkpoint.sh "<id>: <what>"` (it commits and pushes; never use git directly to commit, never force-push, never set a Co-Authored-By trailer).
- Before returning, append a `## Result` section to `.ladder/tasks/<id>.md`: files changed, acceptance pass/fail + summary line, open issues. Then checkpoint.
- Public repo hygiene: no real hostnames, IPs, tokens, emails or personal names (use skein.example.com, "Sam"). Never touch any remote server or running service.
- Machine limit: this laptop has 24 GB RAM. Run at most one test suite at a time; if `uptime` load average is above 8, wait 30 s before running tests.
- Return ≤150 words: files changed, test result line, open issues.
