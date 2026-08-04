# -*- coding: utf-8 -*-
"""test_routine_guard.py — contract-breaking tests for routine_guard.py.

A guard is only worth anything on the one morning it has to save you, so these tests build
a throwaway report file and a fake sessions directory in %TEMP% and then attack the ways a
"wait" betrays you:

  * returning early while the upstream report is still yesterday's (the actual incident);
  * missing report, or a report dated in the future, waved through in silence;
  * mistaking a human's interactive session for a running routine (waits forever);
  * treating a long-dead session as alive;
  * a logger waiting on its own transcript;
  * failing open where it should fail closed, and closed where it should fail open.

Run:   python test_routine_guard.py
Exit:  0 = all pass · 1 = something failed
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "routine_guard.py"
TMP_PREFIX = "harness-rg-"
failures = 0

_spec = importlib.util.spec_from_file_location("routine_guard_mod", SCRIPT)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok and detail:
        print("---- output ----\n" + str(detail).strip() + "\n----------------")


def own_temp(p):
    """Only directories this test created under %TEMP% may ever be removed."""
    if not p:
        return False
    try:
        p = Path(p).resolve()
    except OSError:
        return False
    base = Path(tempfile.gettempdir()).resolve()
    return p != base and base in p.parents and p.name.startswith(TMP_PREFIX)


def cleanup(*paths):
    for p in paths:
        if own_temp(p):
            shutil.rmtree(p, ignore_errors=True)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def mk_report(date_str):
    d = Path(tempfile.mkdtemp(prefix=TMP_PREFIX + "rep-"))
    p = d / "Audit Report.md"
    if date_str:
        write_report(p, date_str)
    return d, p


def write_report(path, date_str):
    Path(path).write_text(
        "# Audit Report\n\n"
        "**Last run:** %s 08:13 - **Scanned:** 156 notes - "
        "**Totals:** 0 violations, 1 suggestion, 0 conflicts\n" % date_str,
        encoding="utf-8")


def mk_sessions():
    return Path(tempfile.mkdtemp(prefix=TMP_PREFIX + "ses-"))


def mk_transcript(root, routine, age_sec=0, sid="deadbeef-0000"):
    """routine=None writes an interactive session (no scheduled-run marker)."""
    d = Path(root) / "project"
    d.mkdir(parents=True, exist_ok=True)
    f = d / ("%s.jsonl" % sid)
    first = ('<scheduled-task name="%s" file="X">\nAutomated run.' % routine) if routine \
        else "hey, can you audit the vault for me"
    f.write_text("\n".join([
        json.dumps({"type": "queue-operation"}),
        json.dumps({"type": "user", "message": {"content": first}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}),
    ]) + "\n", encoding="utf-8")
    if age_sec:
        old = time.time() - age_sec
        os.utime(f, (old, old))
    return f


def run(args, timeout=60):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-B", str(SCRIPT)] + args, capture_output=True,
                       env=env, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode, out


# ------------------------------------------------------------------- wait-report

def case_fresh_report_passes_immediately():
    d, p = mk_report(today())
    t0 = time.time()
    code, out = run(["wait-report", "--report", str(p), "--timeout", "30", "--poll", "5"])
    dt = time.time() - t0
    report("wait-report: today's report -> exit 0 without waiting", code == 0 and dt < 5,
           "code=%s dt=%.1f\n%s" % (code, dt, out))
    cleanup(d)


def case_stale_report_times_out():
    """The incident: the fixer read a four-day-old report and carried on anyway."""
    d, p = mk_report(days_ago(4))
    code, out = run(["wait-report", "--report", str(p), "--timeout", "2", "--poll", "1"])
    report("wait-report: four-day-old report -> exit 3, never exit 0", code == 3,
           "code=%s\n%s" % (code, out))
    cleanup(d)


def case_report_arrives_midwait():
    d, p = mk_report(days_ago(4))

    def later():
        time.sleep(1.5)
        write_report(p, today())

    th = threading.Thread(target=later)
    th.start()
    t0 = time.time()
    code, out = run(["wait-report", "--report", str(p), "--timeout", "20", "--poll", "1"])
    dt = time.time() - t0
    th.join()
    report("wait-report: report refreshed mid-wait -> exit 0, and it really waited",
           code == 0 and dt >= 1.4, "code=%s dt=%.1f\n%s" % (code, dt, out))
    cleanup(d)


def case_missing_report_fails_closed():
    d, p = mk_report(None)
    code, out = run(["wait-report", "--report", str(p), "--timeout", "2", "--poll", "1"])
    report("wait-report: report missing -> exit 2 (fail CLOSED)", code == 2,
           "code=%s\n%s" % (code, out))
    cleanup(d)


def case_future_report_fails_closed():
    d, p = mk_report((datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
    code, out = run(["wait-report", "--report", str(p), "--timeout", "2", "--poll", "1"])
    report("wait-report: report dated tomorrow -> exit 2, not a silent pass", code == 2,
           "code=%s\n%s" % (code, out))
    cleanup(d)


def case_date_parsing():
    d, p = mk_report(None)
    Path(p).write_text("**Last run:** 2026-08-04 10:17 - **Scanned:** 156 notes\n",
                       encoding="utf-8")
    got = rg.report_date(Path(p), rg.DEFAULT_DATE_PATTERN)
    report("date parsing: picks 2026-08-04 out of the real report line", got == "2026-08-04",
           "got=%r" % got)
    cleanup(d)


# -------------------------------------------------------------------- wait-quiet

def case_detection_filters():
    s = mk_sessions()
    mk_transcript(s, "kb-audit-daily", age_sec=0, sid="aaaa-0001")
    mk_transcript(s, "kb-catalog-regen", age_sec=9999, sid="bbbb-0002")
    mk_transcript(s, None, age_sec=0, sid="cccc-0003")
    names = sorted(r["routine"] for r in rg.active_runs(s, idle=180))
    report("detection: only live scheduled runs count (no stale run, no human session)",
           names == ["kb-audit-daily"], "names=%r" % names)
    cleanup(s)


def case_ignores_self():
    s = mk_sessions()
    mk_transcript(s, "kb-perf-log-daily", age_sec=0)
    t0 = time.time()
    code, out = run(["wait-quiet", "--sessions", str(s), "--self", "kb-perf-log-daily",
                     "--timeout", "6", "--poll", "1"])
    dt = time.time() - t0
    report("wait-quiet: ignores its own transcript -> exit 0 immediately",
           code == 0 and dt < 4, "code=%s dt=%.1f\n%s" % (code, dt, out))
    cleanup(s)


def case_blocks_on_other():
    s = mk_sessions()
    mk_transcript(s, "kb-perf-log-daily", age_sec=0, sid="dddd-0004")
    mk_transcript(s, "kb-audit-daily", age_sec=0, sid="eeee-0005")
    code, out = run(["wait-quiet", "--sessions", str(s), "--self", "kb-perf-log-daily",
                     "--timeout", "2", "--poll", "1"])
    report("wait-quiet: another run still live -> exit 3, no truncated measurement",
           code == 3 and "kb-audit-daily" in out, "code=%s\n%s" % (code, out))
    cleanup(s)


def case_only_filter():
    s = mk_sessions()
    mk_transcript(s, "unrelated-export", age_sec=0, sid="ffff-0006")
    code, out = run(["wait-quiet", "--sessions", str(s), "--only", "kb-audit-daily",
                     "--timeout", "2", "--poll", "1"])
    report("wait-quiet --only: routines outside the list do not block", code == 0,
           "code=%s\n%s" % (code, out))
    cleanup(s)


def case_missing_sessions_fails_open():
    ghost = Path(tempfile.gettempdir()) / (TMP_PREFIX + "nonexistent")
    code, out = run(["wait-quiet", "--sessions", str(ghost), "--timeout", "2", "--poll", "1"])
    report("wait-quiet: sessions dir gone -> exit 0 (fail OPEN, never jail the routine)",
           code == 0, "code=%s\n%s" % (code, out))


def case_status_json():
    d, p = mk_report(days_ago(2))
    s = mk_sessions()
    mk_transcript(s, "kb-audit-daily", age_sec=0)
    code, out = run(["status", "--report", str(p), "--sessions", str(s), "--json"])
    try:
        data = json.loads(out)
        ok = (code == 0 and data["report_fresh"] is False
              and data["active"][0]["routine"] == "kb-audit-daily")
    except Exception:                                # noqa: BLE001
        ok = False
    report("status --json: reports report_fresh=false plus the live run", ok,
           "code=%s\n%s" % (code, out))
    cleanup(d, s)


def case_idle_threshold_invariant():
    """The idle threshold must exceed the "in flight" threshold of whatever reads
    transcripts (90s in our setup). Lower it and the guard says "all quiet, go" while the
    reader still considers that session in flight and skips it — the row vanishes for a
    day and nobody is told. That is a silent hole, so a test holds the line, not a comment.
    """
    report("invariant: DEFAULT_IDLE (%ds) > downstream in-flight threshold (90s)"
           % rg.DEFAULT_IDLE, rg.DEFAULT_IDLE > 90, "DEFAULT_IDLE=%s" % rg.DEFAULT_IDLE)


def main():
    case_fresh_report_passes_immediately()
    case_stale_report_times_out()
    case_report_arrives_midwait()
    case_missing_report_fails_closed()
    case_future_report_fails_closed()
    case_date_parsing()
    case_detection_filters()
    case_ignores_self()
    case_blocks_on_other()
    case_only_filter()
    case_missing_sessions_fails_open()
    case_status_json()
    case_idle_threshold_invariant()
    print("\n%s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
