#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routine_guard.py — ordering between scheduled routines, as a mechanism.

The problem this exists for
---------------------------
A self-operating KB ends up with a chain of daily routines: an audit that writes a report,
a fixer that consumes that report, a catalog regeneration that rewrites files the audit
measures, a performance log that reads the *session transcripts* of all the others. That
chain has a real dependency order — and in most setups the only thing enforcing it is the
gap between cron times ("08:00 < 08:20 < 08:30 < 08:45").

That is not a guardrail, it is an assumption about the environment. Ours broke on a
Tuesday: the agent app had been closed past every cron slot and was reopened mid-morning,
so the scheduler fired **all five overdue routines inside two minutes**, in an order that
had nothing to do with their cron times. Three things went wrong at once, and every
routine reported success:

  1. The fixer read the audit report **four minutes before** the audit wrote it, froze a
     four-day-old snapshot into the handling log, and filed a work item on a false premise.
  2. The performance log ran one second after the audit started, scanned a transcript that
     was still being appended to, and recorded "audit: 9s / 149K tokens" for a run that
     actually took 6m08s / 6.1M tokens. That truncated number then travelled as evidence.
  3. The catalog regeneration rewrote the file the audit was measuring at that moment.

The fix is not "spread the cron times further apart" — the same collapse happens with any
gap. The fix is to make the dependency explicit and machine-checked.

Two kinds of waiting
--------------------
  * `wait-report` — wait on **data**: does the audit report carry today's date yet? This is
    the same fact the dependent routine has to establish anyway.
  * `wait-quiet` — wait on **liveness**: is any other scheduled run still writing its
    session transcript?

Deliberately *not* lock files. A lock needs the routine ahead to cooperate with
begin/end, and a routine that dies mid-run (they do) leaves an orphan lock — then you need
a lock-expiry mechanism to repair the lock mechanism. Data does not lie: a report carrying
today's date *is* the proof that the audit finished.

The property that matters: **only downstream routines wait**. Nothing upstream ever waits
on something downstream, so there is no wait cycle and deadlock is impossible by
construction. Keep it that way — if you are tempted to add a wait to the audit itself,
that is the moment you create the cycle.

Usage
-----
    routine_guard.py status      --report PATH [--sessions DIR] [--json]
    routine_guard.py wait-report --report PATH [--timeout 540] [--poll 20]
    routine_guard.py wait-quiet  --sessions DIR [--self NAME] [--only NAME] [--idle 180]

Exit codes: 0 = proceed · 2 = environment error · 3 = timed out waiting.

Fail-open and fail-closed are opposite here, on purpose:
  * `wait-quiet` cannot read the sessions directory → treat as quiet (0). Blocking a
    logger forever is worse than one truncated row that the next run overwrites.
  * `wait-report` cannot read the report → exit 2, and the caller must stand down. This is
    exactly where the incident happened: proceeding without knowing whether the upstream
    stage ran is how a wrong conclusion gets written into the knowledge base.

Zero dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                # noqa: BLE001
        pass

# How a scheduled run identifies itself in the first user message of its transcript.
# Adapt to your agent runtime; the shape is "some marker plus the routine name".
DEFAULT_MARKER = '<scheduled-task name="'
NAME_RE = re.compile(r'name="([^"]+)"')
# One capture group holding the report's date. Override with --date-pattern if your report
# words that line differently (or writes it in another language).
DEFAULT_DATE_PATTERN = r"Last run[^\n]*?(\d{4}-\d{2}-\d{2})"
QUICK_LINES = 60                                     # enough to reach the first user message

# A transcript silent for this long counts as finished. Must stay LARGER than the
# "still in flight" threshold used by whatever reads those transcripts (ours: 90s), or the
# two disagree and a row silently disappears for a day.
DEFAULT_IDLE = 180
DEFAULT_TIMEOUT = 540                                # 9 minutes: fits one tool call
DEFAULT_POLL = 20


# ---------------------------------------------------------------- reading the world

def report_date(path: Path, pattern: str):
    """The date the report claims it was generated, or None if unreadable/unparseable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _first_user_text(fh, limit=QUICK_LINES) -> str:
    for i, line in enumerate(fh):
        if i > limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    return b["text"]
    return ""


def detect_routine(path: Path, marker: str):
    """Routine name if this transcript is a scheduled run, else None.

    Returning None for a human's interactive session is not an optimisation — a logger that
    mistakes your own chat for a running routine waits until it times out, every time.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            t = _first_user_text(fh)
    except OSError:
        return None
    if not t.lstrip().startswith(marker):
        return None
    m = NAME_RE.search(t)
    return m.group(1) if m else "?"


def active_runs(sessions: Path, idle: int = DEFAULT_IDLE, marker: str = DEFAULT_MARKER,
                now: float = None):
    """Scheduled runs whose transcript was written to within `idle` seconds.

    mtime is checked *before* opening anything: a sessions directory holds hundreds of
    files and only the few young ones are worth parsing.
    """
    now = time.time() if now is None else now
    out = []
    try:
        entries = sorted(sessions.glob("*/*.jsonl")) + sorted(sessions.glob("*.jsonl"))
    except OSError:
        return out                                   # fail-open: assume nothing is running
    for f in entries:
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age > idle or age < -60:                  # tolerate small clock skew
            continue
        routine = detect_routine(f, marker)
        if not routine:
            continue
        out.append({"routine": routine, "session": f.stem[:8], "age_sec": int(max(age, 0)),
                    "path": str(f)})
    return sorted(out, key=lambda r: (r["routine"], r["session"]))


# ------------------------------------------------------------------------ commands

def _sleep_until(deadline: float, poll: int) -> bool:
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    time.sleep(min(poll, left))
    return True


def cmd_status(args) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    rd = report_date(Path(args.report), args.date_pattern) if args.report else None
    runs = active_runs(Path(args.sessions), args.idle, args.marker) if args.sessions else []
    if args.json:
        print(json.dumps({"today": today, "report_date": rd, "report_fresh": rd == today,
                          "idle_sec": args.idle, "active": runs}, ensure_ascii=False, indent=1))
        return 0
    print("today (local clock) : %s" % today)
    print("report date         : %s%s" % (rd or "UNREADABLE",
                                          "  -> today's" if rd == today else "  -> NOT today's"))
    if runs:
        print("running (silent < %ds):" % args.idle)
        for r in runs:
            print("  - %-28s session %s, last write %ds ago"
                  % (r["routine"], r["session"], r["age_sec"]))
    else:
        print("running             : none")
    return 0


def cmd_wait_report(args) -> int:
    if not args.report:
        print("ERROR: --report is required", file=sys.stderr)
        return 2
    p = Path(args.report)
    if not p.exists():
        print("ERROR: no report at %s — stand down, do not proceed." % p, file=sys.stderr)
        return 2
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = time.monotonic()
    deadline = t0 + max(args.timeout, 0)
    while True:
        rd = report_date(p, args.date_pattern)
        waited = int(time.monotonic() - t0)
        if rd == today:
            if args.json:
                print(json.dumps({"ok": True, "report_date": rd, "waited_sec": waited}))
            else:
                print("[ok] report carries today's date (%s)%s."
                      % (rd, " after %ds" % waited if waited else ""))
            return 0
        # A date in the future means a skewed clock or a mislabelled report. Waiting cannot
        # help and proceeding quietly is how bad data gets written; stop loudly.
        if rd and rd > today:
            print("ERROR: report dated %s is after today %s (clock skew?) — stand down."
                  % (rd, today), file=sys.stderr)
            return 2
        if not _sleep_until(deadline, args.poll):
            if args.json:
                print(json.dumps({"ok": False, "reason": "timeout", "report_date": rd,
                                  "waited_sec": args.timeout}))
            print("TIMEOUT: waited %ds, report is still %s (need %s)."
                  % (args.timeout, rd or "unreadable", today), file=sys.stderr)
            return 3
        if not args.json:
            print("  ... still %s, waiting (%ds/%ds)"
                  % (rd or "?", int(time.monotonic() - t0), args.timeout))


def cmd_wait_quiet(args) -> int:
    if not args.sessions:
        print("ERROR: --sessions is required", file=sys.stderr)
        return 2
    sessions = Path(args.sessions)
    skip = set(args.self_name or [])
    only = set(args.only or [])
    t0 = time.monotonic()
    deadline = t0 + max(args.timeout, 0)
    while True:
        runs = [r for r in active_runs(sessions, args.idle, args.marker)
                if r["routine"] not in skip and (not only or r["routine"] in only)]
        if not runs:
            waited = int(time.monotonic() - t0)
            if args.json:
                print(json.dumps({"ok": True, "waited_sec": waited}))
            else:
                print("[ok] no other scheduled run active%s."
                      % (" after %ds" % waited if waited else ""))
            return 0
        if not _sleep_until(deadline, args.poll):
            names = ", ".join(sorted({r["routine"] for r in runs}))
            if args.json:
                print(json.dumps({"ok": False, "reason": "timeout", "active": runs}))
            print("TIMEOUT: waited %ds, still running: %s." % (args.timeout, names),
                  file=sys.stderr)
            return 3
        if not args.json:
            print("  ... waiting on: %s"
                  % ", ".join("%s(%ds)" % (r["routine"], r["age_sec"]) for r in runs))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Order scheduled routines by mechanism instead of by cron spacing.")
    ap.add_argument("cmd", choices=["status", "wait-report", "wait-quiet"])
    ap.add_argument("--report", default="", help="path to the audit report note")
    ap.add_argument("--sessions", default="", help="directory holding agent session transcripts")
    ap.add_argument("--marker", default=DEFAULT_MARKER,
                    help="prefix identifying a scheduled run in a transcript")
    ap.add_argument("--date-pattern", default=DEFAULT_DATE_PATTERN,
                    help="regex with one group capturing the report's date")
    ap.add_argument("--idle", type=int, default=DEFAULT_IDLE,
                    help="seconds of silence before a run counts as finished (default %d)"
                         % DEFAULT_IDLE)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="max seconds to wait (default %d)" % DEFAULT_TIMEOUT)
    ap.add_argument("--poll", type=int, default=DEFAULT_POLL, help="seconds between checks")
    ap.add_argument("--self", dest="self_name", action="append", default=[],
                    help="wait-quiet: your own routine name (ignored when counting)")
    ap.add_argument("--only", action="append", default=[],
                    help="wait-quiet: wait on these routines only")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "wait-report":
        return cmd_wait_report(args)
    return cmd_wait_quiet(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
