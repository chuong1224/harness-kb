# Hooks: what the harness owns so the agent doesn't have to remember

`settings.json` is an example [Claude Code](https://docs.claude.com/en/docs/claude-code) hook
configuration wiring three things the harness should own rather than ask an agent to remember:

| Hook | What it does |
|---|---|
| `PostToolUse` | Every read/search/edit appends an event to an activity log — **observability** |
| `PreToolUse` + `SessionEnd` | Every write first claims the file, and blocks if another agent stream holds it — **coordination** (`claim.py`, blueprint H4) |
| `Stop` | When tooling changed during the turn, its test suites run; red blocks the turn from ending — **verification** (`tooling_selfcheck.py`) |

## Activity logging (the observability leg)

The `PostToolUse` entry means **every time the agent reads, searches, or
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

---

## The claim lock (the coordination leg)

The `PreToolUse` entry runs [`../scripts/claim.py`](../scripts/claim.py) **before every write tool
call**, and the `SessionEnd` entry releases whatever the session still held. Unlike the logger this
hook is **synchronous and blocking** — that is the whole point:

- `async` is deliberately absent. An async hook cannot deny anything; the write would already be
  on its way.
- Exiting **2** blocks the tool call and hands the message on stderr back to the model, which then
  sees who holds the file, since when, and what to do about it.
- Exiting **0** allows it. `claim.py` exits 0 on anything it is unsure about — malformed payload,
  no session id, path outside the vault — because a lock that can freeze a session is a lock
  someone will switch off permanently.

Try it in one terminal before trusting it in a session:

```bash
# pretend another agent is editing a shared file
python examples/scripts/claim.py take ops/runbook.md --stream other --why "rewriting rollback"
python examples/scripts/claim.py status

# now the hook refuses your write (exit 2 + an explanation)
echo '{"session_id":"me","tool_name":"Edit","tool_input":{"file_path":"ops/runbook.md"}}' \
  | python examples/scripts/claim.py hook ; echo "exit=$?"

python examples/scripts/claim.py release --all --stream other
```

**Where the state lives:** one small JSON file per stream under `<vault>/.claims/`, plus an
append-only `_events-<host>.jsonl` recording the rare events (a block, a takeover). One writer per
file is what makes this safe inside a synced folder — see blueprint §6 H4 for the reasoning, and
`../scripts/test_claim.py` for the 25 cases that prove it blocks when it should and stays quiet
when it shouldn't.

---

## The tooling gate (the verification leg)

The `Stop` entry runs [`../scripts/tooling_selfcheck.py`](../scripts/tooling_selfcheck.py) at the
end of every turn. It exists because of a question the other two legs raise: the scripts keeping
your KB honest have tests — **who runs them?**

- Nothing in the tooling changed since the last green run → it exits in ~0.2s having done nothing.
- Tooling changed and every suite passes → it records a new green marker and lets the turn end.
- Tooling changed and a suite is **red** → exit 2, which blocks the turn from ending and hands the
  failure to the model to fix.

The suite is **discovered**, never listed: any `*/attachments/test_*.py` in the vault is in it. A
list you must remember to append to is the same failure mode one level up.

```bash
python examples/scripts/tooling_selfcheck.py list          # what would run, and what has no test
python examples/scripts/tooling_selfcheck.py run           # run everything now
python examples/scripts/tooling_selfcheck.py run --if-stale  # what the hook calls
```

Two properties to keep if you reimplement it: a **red run must not update the green marker** (or
the gate goes quiet exactly when it matters), and the hook must **never block twice in one turn**
— `stop_hook_active` in the payload tells you it already did. See `../scripts/test_tooling_selfcheck.py`
for the 20 cases, including the one that matters most: a mistyped `--vault` used to find no suites,
report green, and silence the gate forever.
