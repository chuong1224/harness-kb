# Activity-logging hook (the observability leg)

`settings.json` is an example [Claude Code](https://docs.claude.com/en/docs/claude-code) hook
configuration. It wires a `PostToolUse` hook so that **every time the agent reads, searches, or
edits a note, an event is appended to an activity log — automatically, without the agent having
to remember to log.**

That is the point: observability should be a property of the *harness*, not a discipline the
agent must uphold. If logging depends on the agent choosing to log, it will drift; if the runtime
logs on every tool call, the observability loop closes on its own.

## How it works

- `matcher` selects which tools fire the hook (read/search/edit tools here).
- `command` runs your logger. Point it at a small script that appends one JSON line per event
  (timestamp, event type, file). `$CLAUDE_PROJECT_DIR` expands to the project root.
- `async: true` keeps the hook off the critical path — it never slows the agent down.
- `timeout` caps the hook so a stuck logger can't hang the session.

## The logger (`scripts/log_activity.py`, not included)

Keep it tiny and defensive:

1. Read the tool payload from **stdin as bytes**, then `.decode("utf-8")` — reading text mode on
   some platforms corrupts non-ASCII filenames, which then fail to match your notes.
2. Filter to `.md` files inside the vault.
3. Append one line to an activity log **outside** any cloud-synced folder (avoid sync churn and
   multi-machine write conflicts).

An append-only event log is enough to power a live activity view, a "hot notes" heatmap, and a
retrieval-efficiency report (long chains and re-reads are the signal for H3 in the blueprint).

## Design rule

**The agent only WRITES the log. It never runs the viewer/server.** Separating "emit events" from
"serve the UI" is what keeps a single well-behaved reader and avoids zombie processes holding a
port — a small but load-bearing guardrail.
