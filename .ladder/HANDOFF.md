# Handoff: 2026-11-11
Phase: v0.1.0 deployed privately with OAuth; five members documented (claude point, muse, grok, gemini, chatgpt)
In flight: none
Next action: none required. Open item: Claude point check-in not scheduled (by choice).
Blocked on human: none
Workflow: none running (runIds in LEDGER)
Notes:
- Machine-, account- and host-specific notes live only in gitignored *.local.md files. Never put them in tracked files.
- Upgrades: back up the DB with sqlite backup, `pip install --force-reinstall --no-deps git+…@<commit>`, check `skein status` loads the config, then restart.
