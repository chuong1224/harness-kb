#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tooling_selfcheck.py - the gate that RUNS the tests guarding your knowledge-base tooling.

THE PROBLEM. Once the scripts that keep a KB honest live inside the vault (see the
blueprint's "the tooling has to live in the vault" rule), they need tests - and those
tests need someone to run them. In practice nobody does. A test suite nobody runs rots
silently: it does not fail, it simply stops being evidence, and you find out months later
that the gate you trusted has been vacuous for weeks.

THE FIX. Two layers, and neither of them asks an agent to remember anything:

  1. **A runner that DISCOVERS its own suite.** Every `*/attachments/test_*.py` in the
     vault is picked up and run. There is deliberately no hand-maintained list of tests -
     a list you must remember to append to is the same failure mode one level up. Add a
     tool with a test beside it and it is in the suite; that is the whole registration
     protocol.
  2. **A `Stop` hook that calls the runner at the end of every agent turn.** Nothing
     changed since the last green run -> it exits in ~0.2s having done nothing. Tooling
     was touched and a suite is red -> **exit 2**, which blocks the turn from ending and
     hands the failure back to the model to fix.

"Changed" is mtime+size over every `attachments/*.py` (tools included: editing a tool
invalidates yesterday's green run just as much as editing its test), compared against the
fingerprint of the last GREEN run. That marker is a per-machine, per-vault cache kept
OUTSIDE the vault: two machines edit at different times, and a marker inside a synced
folder is one more file for the sync to conflict over. The default filename includes a
hash of the canonical vault root, so two vaults on one host cannot read or overwrite each
other's coverage mark. Losing the cache costs one redundant run and can never produce a
wrong answer.

THREE SAFETY RULES, each learned the hard way:
  * **One runner at a time** (a lock). Suites that exercise a vault by mutating real
    documents and restoring them will corrupt each other if two runs overlap. If the lock
    is taken, skip the run entirely rather than queue - the green marker is not written,
    so the next turn tries again.
  * **A red run never updates the green marker.** Otherwise the gate goes quiet
    immediately after the first failure - exactly when you need it loudest.
  * **Fail open, and never loop.** Unknown payload, no vault, runner error -> allow. If
    the hook already blocked once in this turn (`stop_hook_active`), report but let the
    turn end. A gate that can trap a session is a gate someone disables permanently.

MEASURE THE SUITE, DO NOT ASK IT. A zero exit proves nothing was raised; it does not
prove anything was checked. A suite that skips quietly (`if not condition: pass`), or that
loses assertions to a careless edit, still exits zero while the gate stamps another green
run over coverage that has evaporated. So every suite prints ONE LINE PER ASSERTION, the
runner counts those lines, and a count below the last green run BLOCKS - deleting five
cases and silently skipping five look identical from outside, and a drop is an observable
fact rather than an intent to be guessed. The way out is not to switch the gate off but
`accept --reason "..."`, which lowers the mark and records why.

And because a gate built on deltas cannot see the value that has no delta, a suite that
runs green while printing no assertion at all is refused too - its count is 0, and 0
cannot fall. That one gets NO acceptance path: lowering a mark concedes coverage fell for
a reason, while silence means the instrument never reached that suite at all.

BROKEN CODE IS NOT BROKEN MEASUREMENT. A suite can be red for reasons that have nothing
to do with the code it tests, and reporting those as ordinary failures sends people to
edit healthy code. Two get their own verdict:
  * `MISSING-LIB` - the interpreter running the tests lacks a library.
  * `LOCKED-FILE` - a document the suite reads is held open by a desktop application.
Both BLOCK exactly as hard as a red suite and record no green mark; the label only steers
the repair. Both are earned by RE-DERIVING the fact at classification time (re-opening the
file, reading the suite's own declaration) rather than by pattern-matching the error text
- a fixture can print any string it likes, and the same exception arrives from causes the
label does not cover. A verdict a gate states as fact must be measured, not parsed.

USAGE
    tooling_selfcheck.py run [--if-stale]     # run everything; exit 1 if any suite fails
    tooling_selfcheck.py list                 # suites found + tools with no test beside them
    tooling_selfcheck.py hook-stop            # for the Stop hook (payload on stdin)
    tooling_selfcheck.py accept --reason "."  # lower the coverage mark, on the record
    tooling_selfcheck.py forget               # drop the green marker, forcing a full run

Flags: --vault PATH - --state PATH - --timeout SECONDS (default 300, per suite) - --json
Kill switch: KB_TOOLING_GATE_OFF=1 (the hook allows everything; the runner still works).

Exit: 0 = green / nothing to do - 1 = a suite FAILED, could not be measured, or coverage
fell - 2 = HOOK BLOCKED the turn.
Python 3.8+, standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass

HOST = socket.gethostname().split(".")[0] or "unknown"
SKIP_DIRS = {".git", ".obsidian", ".claims", ".trash", "__pycache__", "node_modules",
             "backup", "vendor"}
TEST_RE = re.compile(r"(?i)^test_.*\.py$")
ROOT_MARKERS = [m for m in (os.environ.get("KB_CLAIM_ROOT_MARKER"), ".obsidian", ".kb-root") if m]
# Where "vault tooling" is declared to live. The missing-test report is scoped to these
# so one-off scripts elsewhere in the vault are not counted as test debt. Override with
# KB_TOOLING_ROOTS="Ops,Meta" (empty = report on the whole vault).
_roots = os.environ.get("KB_TOOLING_ROOTS")
TOOLING_ROOTS = tuple(r.strip() for r in _roots.split(",") if r.strip()) if _roots is not None \
    else ("Ops",)
DEFAULT_TIMEOUT = int(os.environ.get("KB_TOOLING_TIMEOUT") or 300)


# -------------------------------------------------------------------------- basics

def find_vault(start: Path):
    try:
        cands = [start, *start.parents]
    except (OSError, ValueError):
        return None
    for cand in cands:
        for marker in ROOT_MARKERS:
            try:
                if (cand / marker).exists():
                    return cand
            except OSError:
                continue
    return None


def is_vault(p) -> bool:
    return bool(p) and any((Path(p) / m).exists() for m in ROOT_MARKERS)


def _vault_key(vault: Path) -> str:
    """Stable, filesystem-aware namespace without exposing the vault path in a filename."""
    try:
        root = os.path.normcase(str(vault.resolve()))
    except (OSError, RuntimeError):
        root = os.path.normcase(os.path.abspath(os.fspath(vault)))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def state_path(vault: Path, explicit: str = "") -> Path:
    """Per-machine, per-vault cache outside the vault (see module docstring)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("KB_TOOLING_STATE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "kb-tooling-selfcheck" / (
        "state-%s-%s.json" % (HOST, _vault_key(vault)))


def gate_off() -> bool:
    return (os.environ.get("KB_TOOLING_GATE_OFF") or "").strip() not in ("", "0", "false", "no")


def walk_attachments(vault: Path):
    """Every .py living in a directory named `attachments`."""
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if os.path.basename(root).lower() != "attachments":
            continue
        for name in files:
            if name.lower().endswith(".py"):
                yield Path(root) / name


def discover(vault: Path):
    """The suite is whatever matches `attachments/test_*.py`. Sorted, so two machines
    run the same suites in the same order."""
    return sorted((p for p in walk_attachments(vault) if TEST_RE.match(p.name)),
                  key=lambda p: str(p).lower())


def tools_without_test(vault: Path):
    out = []
    for p in walk_attachments(vault):
        if TEST_RE.match(p.name):
            continue
        rel = str(p.relative_to(vault)).replace("\\", "/")
        if TOOLING_ROOTS and not any(rel.startswith(r + "/") for r in TOOLING_ROOTS):
            continue
        if not (p.parent / ("test_%s" % p.name)).is_file():
            out.append(rel)
    return sorted(out)


def signature(vault: Path) -> dict:
    """Fingerprint of ALL tooling: touching a tool invalidates the last green run just as
    much as touching a test."""
    sig = {}
    for p in walk_attachments(vault):
        try:
            st = p.stat()
        except OSError:
            continue
        sig[str(p.relative_to(vault)).replace("\\", "/")] = [int(st.st_mtime), st.st_size]
    return sig


def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass                                     # a broken cache must not break the gate


def stale(vault: Path, path: Path):
    """(should_run, reason) - current fingerprint vs. the last GREEN run."""
    st = load_state(path)
    old = st.get("signature")
    if not isinstance(old, dict) or not old:
        return True, "no green run recorded on this machine yet"
    cur = signature(vault)
    if cur == old:
        return False, "nothing changed since the green run of %s" % (
            time.strftime("%d %b %H:%M", time.localtime(st.get("last_ok") or 0)))
    changed = sorted(set(cur) ^ set(old)) or sorted(k for k in cur if cur[k] != old.get(k))
    return True, "changed: " + ", ".join(changed[:3]) + ("..." if len(changed) > 3 else "")


# ------------------------------------------- broken code vs. broken measurement

# Import name != pip name for a handful of packages; this is the one thing that cannot be
# derived, so it is written down. Anything absent here installs under its own name.
PIP_NAMES = {"yaml": "PyYAML", "PIL": "pillow", "docx": "python-docx",
             "win32com": "pywin32", "pythoncom": "pywin32", "fitz": "PyMuPDF"}
MISSING_RE = re.compile(r"No module named '([\w.]+)'")

# The declaration protocol: a suite prints `[SKIP] <reason>` for EVERY item it could not
# measure. Matched per LINE and anchored to the line start on purpose - any suite may
# print the words "No module named" inside its own toy data (the suite next door really
# does), so only a suite's own declaration counts. Leading whitespace is allowed because a
# suite that prints the marker from inside an indented block, or that forwards a child
# runner's output, is still declaring honestly; the BRACKETS are what stop a false count.
SKIP_RE = re.compile(r"^[ \t]*\[SKIP\][ \t]*(.*)$", re.M)

# One line per assertion, in any of the spellings suites actually use: `[PASS] x`,
# `PASS x`, indented variants, `PASS - x`. A separator character is REQUIRED right after
# the label so that `PASSED`, `PASS*` (a runner label) and a closing `ALL PASS` are not
# counted. FAIL lines count too: a red turn is already blocked at another door, and
# counting them keeps the number from dropping merely because a suite went red.
ASSERTION_RE = re.compile(r"(?m)^[ \t]*(?:\[(?:PASS|FAIL)\]|PASS|FAIL)(?=[ \t·:])")
# The fifth spelling: suites that use bare `assert` and print one roll-up `PASS 10/10`.
# Counting lines would peg them at 1 forever - delete nine scenarios and the number never
# moves. Take the LEFT side (how many actually ran), never the denominator: the denominator
# is a hand-typed constant that does not budge when a case is deleted.
# `\r?` is not decoration: a child process on Windows ends its lines with CRLF, so without
# it the end-of-line branch never matches and every roll-up suite silently counts as one.
ROLLUP_RE = re.compile(
    r"(?m)^[ \t]*(?:PASS|FAIL)[ \t]+(\d+)[ \t]*/[ \t]*\d+(?=[ \t]*(?:·|\r?$))")

# A desktop office application holds its document open hard enough that both `open(rb)`
# and a plain file copy are refused, so merely LOOKING at a spreadsheet turns every suite
# that reads it red. The extension list is deliberately narrow: a PermissionError on a
# `.py` or `.json` is a different animal (filesystem permissions, another process writing)
# and must not borrow a diagnosis that tells people to close an application.
LOCKED_RE = re.compile(
    r"PermissionError:[^\n]*?['\"]([^'\"\n]+\.(?:xlsx|xlsm|xltx|xls|docx|docm|dotx|doc"
    r"|pptx|ppt))['\"]", re.I)


def skips(res: dict) -> list:
    """The items a suite DECLARED it could not measure. Empty = it measured everything."""
    return [s.strip() for s in SKIP_RE.findall(res.get("output") or "")]


def missing_module(res: dict) -> str:
    """The library this interpreter lacks, or "" if that is not what happened.

    Tests are run with `sys.executable` - whichever `python` invoked the gate. That is
    routinely not the interpreter a human would get from PATH, so a suite can be red while
    the code it tests is perfectly healthy. Filed as FAIL, it invites the next session to
    go and "fix" working code.

    Caught from BOTH directions, because a missing library shows up two opposite ways:
      * red   - the suite dies on `ModuleNotFoundError` at import time;
      * FALSE GREEN - the suite catches ImportError, prints `[SKIP]`, exits 0, and the
        runner counts it a pass while nothing was measured. This one is the dangerous
        half: a red suite at least argues back."""
    if not res["ok"]:
        m = MISSING_RE.search(res.get("output") or "")
        if m:
            return m.group(1).split(".")[0]
    for reason in skips(res):            # green, but declared it skipped for want of a lib
        m = MISSING_RE.search(reason)
        if m:
            return m.group(1).split(".")[0]
    return ""


def _still_locked(p: Path) -> bool:
    """Take the measurement AGAIN; do not trust the text in the output.

    Any suite can print the string `PermissionError` inside its own fixtures - the exact
    trap `MISSING_RE` has to dodge by reading only a suite's own declaration. Here there is
    something better than a convention: open the file right now. Still refused, and the
    lock is an observed fact. Opens fine, and that PermissionError came from somewhere this
    label does not cover, so it stays a plain failure."""
    try:
        with open(p, "rb") as f:
            f.read(1)
        return False
    except PermissionError:
        return True
    except OSError:
        return False                     # absent, or a different error: not this case


def locked_file(res: dict) -> str:
    """Path of the held-open document behind a red suite; "" if that is not the cause.

    Memoised onto the result: `classify` is called several times per suite, each call would
    otherwise touch the disk again - and worse, a file closed midway would hand back two
    different verdicts for the same run."""
    if "_locked" in res:
        return res["_locked"]
    res["_locked"] = ""
    if not res.get("ok"):
        home = Path(res["path"]).parent      # suites run with cwd = their own folder
        for m in LOCKED_RE.findall(res.get("output") or ""):
            p = Path(m.replace("\\\\", "\\"))    # the path came out of a repr: undo doubling
            if not p.is_absolute():
                p = home / p
            if _still_locked(p):
                res["_locked"] = str(p)
                break
    return res["_locked"]


def who_holds_it() -> str:
    """A command that names the culprit. PRINTED, never run: a gate has no business
    touching a user's processes, and the reader deserves to see what they were told to do."""
    if os.name == "nt":
        return ('powershell -NoProfile -Command "Get-Process EXCEL,WINWORD,POWERPNT '
                '-ErrorAction SilentlyContinue | Select-Object Id,ProcessName,MainWindowTitle"')
    return "lsof -- <the file named above>"


def classify(res: dict) -> str:
    """One label per suite. FIVE of them, and they must not blur together:
    PASS (measured, green) - PASS* (green, with declared gaps) - MISSING-LIB (measurement
    broken, BLOCKS) - LOCKED-FILE (measurement broken, BLOCKS) - FAIL (code broken, BLOCKS)."""
    if missing_module(res):
        return "MISSING-LIB"
    if locked_file(res):
        return "LOCKED-FILE"
    if not res["ok"]:
        return "FAIL"
    return "PASS*" if skips(res) else "PASS"


def count_assertions(res: dict) -> int:
    """How many assertions the suite ACTUALLY ran - counted from its output, not asked."""
    out = res.get("output") or ""
    rollups = [int(n) for n in ROLLUP_RE.findall(out)]
    # A roll-up line was already counted as 1 above; give that 1 back, add what it declares.
    return len(ASSERTION_RE.findall(out)) - len(rollups) + sum(rollups)


def is_mute(res: dict) -> bool:
    """A MUTE suite: ran, exited 0, and said nothing at all.

    This is precisely where the coverage measurement cannot reach. It catches a count that
    FALLS, and a mute suite sits at 0 forever - nothing to fall from. Delete an entire test
    class out of one and no number moves.

    Both exclusions are deliberate, so this fires only where it is the sole signal:
      * `ok` - red and unmeasurable suites usually die before printing anything; they are
        blocked at another door already, and shouting here would blame the measurement.
      * no `[SKIP]` - a suite that declared its gaps has spoken through the other channel.
        "Not measured yet" is not the same as silent."""
    return bool(res.get("ok")) and not count_assertions(res) and not skips(res)


def coverage_delta(old: dict, new: dict, on_disk) -> tuple:
    """Compare against the last green mark. Returns (dropped, vanished).

    * dropped  - the suite is still there but ran fewer assertions: a silent skip, or
      cases deleted.
    * vanished - the whole SUITE is gone from disk. Deleting a test file is a coverage
      loss too, and the blindest kind: no suite, no output, nothing to inspect. Only
      suites that once measured something (>0) count, so a suite that was always silent
      does not shout when it is finally cleaned up.

    There is deliberately NO tolerance threshold. A tolerance is exactly the room in which
    a couple of cases go missing per round, and a couple of cases per round is how coverage
    actually erodes."""
    dropped = [(t, old[t], new[t]) for t in sorted(new)
               if isinstance(old.get(t), int) and new[t] < old[t]]
    vanished = sorted(t for t, n in old.items()
                      if isinstance(n, int) and n > 0 and t not in on_disk)
    return dropped, vanished


def install_command(mods) -> str:
    """Patch the interpreter that is ACTUALLY running the tests - not some other python."""
    pkgs = sorted({PIP_NAMES.get(m, m) for m in mods if m})
    return '"%s" -m pip install %s' % (sys.executable, " ".join(pkgs))


# ---------------------------------------------------------------------------- lock

def _busy_lock(path: Path):
    """Only ONE runner at a time on this machine. Not cosmetic: a suite that mutates real
    documents and restores them will have its restore clobbered by a parallel run. If the
    lock is held, SKIP this round instead of queueing - the green marker stays unwritten,
    so the next turn simply tries again."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lf = open(str(path) + ".lock", "a+")
    except OSError:
        return None, True                        # cannot lock -> run anyway, never block work
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            for _ in range(100):
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    return lf, True
                except OSError:
                    time.sleep(0.01)
            lf.close()
            return None, False
        import fcntl
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lf, True
        except OSError:
            lf.close()
            return None, False
    except Exception:                            # noqa: BLE001
        return lf, True


def _unlock(lf) -> None:
    if not lf:
        return
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:                            # noqa: BLE001
        pass
    try:
        lf.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- running

def run_one(test: Path, timeout: int) -> dict:
    t0 = time.perf_counter()
    # Make the INSTRUMENT deterministic before trusting its numbers. Output is decoded as
    # UTF-8 below, but a child process inherits the platform's locale encoding for stdout -
    # so a separator character comes back mangled, a counting pattern misses, and the same
    # suite scores differently depending on which shell launched the runner. A suite that
    # only passes on one operator's machine has not been measured.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        # -B: do not litter the vault with __pycache__ nobody will ever clean up.
        r = subprocess.run([sys.executable, "-B", str(test)], capture_output=True,
                           cwd=str(test.parent), timeout=timeout, env=env)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = "exceeded %ds - counted as FAILED" % timeout, 124
    except OSError as e:
        out, code = "could not run: %r" % (e,), 125
    return {"test": test.name, "path": str(test), "ok": code == 0, "code": code,
            "seconds": round(time.perf_counter() - t0, 1), "output": out}


def run_all(vault: Path, timeout: int, quiet: bool = False):
    """Run every discovered suite. Returns (results, the suites that were on disk).

    The suite list is returned as well as the results because a suite that VANISHED can
    only be noticed by comparing what is on disk against the last green mark - and a
    deleted file produces no result to inspect."""
    tests = discover(vault)
    results = []
    for t in tests:
        res = run_one(t, timeout)
        results.append(res)
        if not quiet:
            # Print the assertion count on every line. It is the number the next run
            # compares against, so showing it each time lets a reader watch coverage
            # move before the gate ever has to shout about it.
            print("%-11s %-28s (%.1fs)  %d assertions  %s"
                  % (classify(res), res["test"], res["seconds"],
                     count_assertions(res), res["path"]))
            for reason in skips(res):            # declared gaps must be VISIBLE
                print("      ~ skipped: %s" % reason)
            if not res["ok"]:
                for line in res["output"].strip().splitlines()[-12:]:
                    print("      | " + line)
    return results, tests


def cmd_run(args, vault: Path, details=None) -> int:
    sp = state_path(vault, args.state)
    if args.if_stale:
        need, why = stale(vault, sp)
        if not need:
            if not args.json:
                print("Skipped: %s." % why)
            return 0
        if not args.json:
            print("Running because %s" % why)

    lf, got = _busy_lock(sp)
    if not got:
        if not args.json:
            print("Skipping this round: another runner is active on this machine.")
        return 0
    try:
        t0 = time.perf_counter()
        # Read the mark BEFORE running: save_state below overwrites it, and the old
        # numbers are exactly what this run has to be compared against.
        st_old = load_state(sp)
        mark_old = st_old.get("counts") if isinstance(st_old.get("counts"), dict) else {}
        reason = (getattr(args, "reason", "") or "").strip()
        results, tests = run_all(vault, args.timeout, quiet=args.json)
        # Broken code and broken measurement both BLOCK - but they get separate names,
        # because the repair for one is the opposite of the repair for the other.
        no_lib = [r for r in results if classify(r) == "MISSING-LIB"]
        locked = [r for r in results if classify(r) == "LOCKED-FILE"]
        bad = [r for r in results if classify(r) == "FAIL"]
        mods = sorted({missing_module(r) for r in no_lib})
        # Green, but with declared gaps: NOT blocked (the reason may be unfixable on this
        # machine), yet never allowed to go invisible either.
        partial = [r for r in results if classify(r) == "PASS*"]
        skipped_items = sum(len(skips(r)) for r in results)
        # Coverage is only compared when the run is otherwise clean: a red suite usually
        # dies early and prints fewer assertions, and comparing that would report a "drop"
        # whose real culprit is the failure already blocked at another door.
        counts = {r["test"]: count_assertions(r) for r in results}
        clean = not bad and not no_lib and not locked
        dropped, vanished = (coverage_delta(mark_old, counts, {t.name for t in tests})
                             if clean else ([], []))
        mute = [r["test"] for r in results if is_mute(r)]
        if details is not None:
            # So the Stop hook can name the actual cause instead of saying "something is
            # red" - the four causes have four different repairs.
            details.clear()
            details.update({"failed": [r["test"] for r in bad],
                            "missing_lib": [r["test"] for r in no_lib],
                            "missing_modules": mods,
                            "locked": [r["test"] for r in locked],
                            "locked_files": sorted({locked_file(r) for r in locked}),
                            "coverage_dropped": [t for t, _a, _b in dropped],
                            "vanished": list(vanished), "mute": list(mute)})

        # `vanished` has to survive this door: a vault whose suites were all deleted also
        # lands in "no suites found", and returning 0 here is silence at the exact moment
        # the gate should be loudest.
        if not results and not vanished:
            if not args.json:
                print("No suites found (%s/**/attachments/test_*.py)." % vault.name)
            save_state(sp, {"last_ok": time.time(), "host": HOST, "tests": 0,
                            "signature": signature(vault)})
            return 0
        if clean and not mute and (not (dropped or vanished) or reason):
            mark = {"last_ok": time.time(), "host": HOST, "tests": len(results),
                    "skipped": skipped_items, "counts": counts,
                    "signature": signature(vault)}
            if reason and (dropped or vanished):
                # Lowering the mark is a deliberate act and must leave a trace: a mark that
                # fell for a forgotten reason is indistinguishable next time from one that
                # fell because something slipped through.
                history = [x for x in (st_old.get("accepted") or []) if isinstance(x, dict)]
                history.append({"when": time.strftime("%Y-%m-%d %H:%M"), "reason": reason,
                                "dropped": [{"test": t, "was": a, "now": b}
                                            for t, a, b in dropped],
                                "vanished": list(vanished)})
                mark["accepted"] = history[-5:]
            elif st_old.get("accepted"):
                mark["accepted"] = st_old["accepted"]
            save_state(sp, mark)

        if no_lib and not args.json:
            print("\n" + "=" * 72, file=sys.stderr)
            print("BROKEN MEASUREMENT, NOT BROKEN CODE - %d suite(s) red for want of a"
                  " library:" % len(no_lib), file=sys.stderr)
            for r in no_lib:
                print("   - %s <- needs `%s`" % (r["test"], missing_module(r)), file=sys.stderr)
            print("   Interpreter running the tests: %s" % sys.executable, file=sys.stderr)
            print("   Do NOT edit the code. Patch that exact interpreter:", file=sys.stderr)
            print("   %s" % install_command(mods), file=sys.stderr)
            print("=" * 72, file=sys.stderr)
        if locked and not args.json:
            print("\n" + "=" * 72, file=sys.stderr)
            print("BROKEN MEASUREMENT, NOT BROKEN CODE - %d suite(s) red because a document"
                  " is HELD OPEN:" % len(locked), file=sys.stderr)
            for r in locked:
                print("   - %s <- %s" % (r["test"], Path(locked_file(r)).name), file=sys.stderr)
            print("   RE-CHECKED just now: the file still refuses to open for reading.",
                  file=sys.stderr)
            print("   Desktop office applications lock hard - reading and copying are both",
                  file=sys.stderr)
            print("   refused, so there is no way around this in code. Close it and re-run;",
                  file=sys.stderr)
            print("   to find the holder:", file=sys.stderr)
            print("   %s" % who_holds_it(), file=sys.stderr)
            print("=" * 72, file=sys.stderr)
        if partial and not args.json:
            print("\n~ NOT FULLY MEASURED (not blocking): %d item(s) skipped across %d green"
                  " suite(s) -" % (skipped_items, len(partial)))
            for r in partial:
                for why_skip in skips(r):
                    print("   - %s: %s" % (r["test"], why_skip))
            print("  'green' here means the REST is green, not that everything was measured.")
        if (dropped or vanished) and not args.json:
            print("\n" + "=" * 72, file=sys.stderr)
            if reason:
                print("COVERAGE FELL - ACCEPTED: %s" % reason, file=sys.stderr)
            else:
                print("COVERAGE FELL - every suite is green, but they measured LESS than at"
                      " the last green run:", file=sys.stderr)
            for t, a, b in dropped:
                print("   - %s: %d -> %d assertions (lost %d)" % (t, a, b, a - b),
                      file=sys.stderr)
            for t in vanished:
                print("   - %s: the whole SUITE is gone from disk (last mark: %d assertions)"
                      % (t, mark_old.get(t, 0)), file=sys.stderr)
            if not reason:
                print("   Green while measuring less = a silent skip, or assertions deleted."
                      " There is no", file=sys.stderr)
                print("   [SKIP] line to read, so this number is the ONLY signal - do not"
                      " wave it through.", file=sys.stderr)
                print("   Restore the coverage, or lower the mark ON THE RECORD:",
                      file=sys.stderr)
                print('   python "%s" accept --reason "..."' % __file__, file=sys.stderr)
            print("=" * 72, file=sys.stderr)
        if mute and not args.json:
            print("\n" + "=" * 72, file=sys.stderr)
            print("MUTE SUITE(S) - %d ran, exited 0, and asserted nothing:" % len(mute),
                  file=sys.stderr)
            for t in mute:
                print("   - %s" % t, file=sys.stderr)
            print("   The coverage measurement cannot reach these: their count is 0, and 0"
                  " cannot fall -", file=sys.stderr)
            print("   an entire test class could be deleted without moving a number. That is"
                  " a hole in the", file=sys.stderr)
            print("   net itself, so it is refused at the door rather than discovered after"
                  " the loss.", file=sys.stderr)
            print("   Fix: print ONE line per assertion - `[PASS] <name>` / `[FAIL] <name>`."
                  " Deliberate", file=sys.stderr)
            print("   gaps get `[SKIP] <reason>` - once declared, a suite is no longer mute.",
                  file=sys.stderr)
            print("=" * 72, file=sys.stderr)

        if args.json:
            print(json.dumps({"ok": clean and not mute
                              and (not (dropped or vanished) or bool(reason)),
                              "seconds": round(time.perf_counter() - t0, 1),
                              "missing_libs": mods,
                              "locked_files": {r["test"]: locked_file(r) for r in locked},
                              "skipped": {r["test"]: skips(r) for r in results if skips(r)},
                              "assertions": counts,
                              "coverage_dropped": [{"test": t, "was": a, "now": b}
                                                   for t, a, b in dropped],
                              "vanished": list(vanished), "mute": mute,
                              "results": [{k: v for k, v in r.items() if k != "output"}
                                          for r in results]}, ensure_ascii=False, indent=1))
        else:
            print("\nRESULT (%.1fs): %s%s%s" % (
                time.perf_counter() - t0,
                "ALL PASS, %d suites" % len(results) if clean
                else "FAILED %d/%d - %s" % (len(bad), len(results),
                                            ", ".join(r["test"] for r in bad)) if bad
                else "%d/%d suites could not be measured" % (len(no_lib) + len(locked),
                                                             len(results)),
                ("" if not mods else
                 " - MISSING LIBRARY %s (broken measurement, not broken code)"
                 % ", ".join(mods))
                + ("" if not locked else
                   " - DOCUMENT HELD OPEN: %s (broken measurement, not broken code)"
                   % ", ".join(sorted({Path(locked_file(r)).name for r in locked}))),
                ("" if not skipped_items else " - %d item(s) skipped" % skipped_items)
                + (" - %d assertions" % sum(counts.values()))
                + ("" if not (dropped or vanished) else
                   " - COVERAGE FELL in %d suite(s)" % (len(dropped) + len(vanished)))
                + ("" if not mute else " - %d MUTE" % len(mute))))

        # Coverage loss BLOCKS, like a red suite. Reporting without blocking would rebuild
        # the very silent channel this measurement exists to close: on the Stop-hook path,
        # exit 0 means nobody reads a single line of the above. The way out is `accept
        # --reason`, not switching the gate off - lowering the mark is fine, lowering it
        # quietly is not.
        #
        # A MUTE suite blocks too, and deliberately gets NO acceptance path. The two are
        # different admissions: lowering a mark says coverage fell for a reason, while
        # silence says the instrument never reached that suite at all - "accepting" that
        # is agreeing to let it drift forever. The fix costs one print statement.
        coverage_blocks = bool(dropped or vanished) and not reason
        return 1 if (bad or no_lib or locked or coverage_blocks or mute) else 0
    finally:
        _unlock(lf)


def cmd_list(args, vault: Path) -> int:
    tests = discover(vault)
    missing = tools_without_test(vault)
    if args.json:
        print(json.dumps({"tests": [str(t.relative_to(vault)).replace("\\", "/") for t in tests],
                          "tools_without_test": missing}, ensure_ascii=False, indent=1))
        return 0
    print("Suites discovered (%d):" % len(tests))
    for t in tests:
        print("  - %s" % str(t.relative_to(vault)).replace("\\", "/"))
    scope = "/".join(TOOLING_ROOTS) if TOOLING_ROOTS else "the whole vault"
    print("\nTools under %s with no test beside them (%d) - informational, NOT a failure:"
          % (scope, len(missing)))
    for m in missing:
        print("  - %s" % m)
    need, why = stale(vault, state_path(vault, args.state))
    print("\nGreen marker: %s (%s)" % ("needs a run" if need else "still valid", why))
    return 0


def cmd_forget(args, vault: Path) -> int:
    sp = state_path(vault, args.state)
    try:
        sp.unlink()
        print("Dropped the green marker: %s" % sp)
    except OSError:
        print("No green marker to drop (%s)." % sp)
    return 0


def read_payload() -> dict:
    """Read bytes and decode UTF-8 explicitly: a child process on Windows inherits a
    locale stdin encoding, so text mode mangles non-ASCII paths in the payload."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:                            # noqa: BLE001
        return {}
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def cmd_hook_stop(args) -> int:
    """Stop hook: block the turn from ending when tooling just changed and a suite is red."""
    if gate_off():
        return 0
    d = read_payload()
    vault = Path(args.vault) if args.vault else find_vault(Path(d.get("cwd") or os.getcwd()))
    if not vault:
        vault = find_vault(Path(__file__).resolve())
    if not vault:
        return 0
    sp = state_path(vault, args.state)
    need, why = stale(vault, sp)
    if not need:
        return 0                                 # the quiet path: ~0.2s per turn

    ns = argparse.Namespace(state=args.state, if_stale=False, json=False,
                            timeout=args.timeout, reason="")
    details = {}
    if cmd_run(ns, vault, details) == 0:
        return 0
    again = bool(d.get("stop_hook_active"))
    # Name the cause. "A suite is red" sent at a locked document, or at coverage that
    # quietly fell, is how people end up editing code that was never broken.
    if details.get("mute"):
        headline = "a tooling suite ran green while asserting NOTHING (%s)" \
                   % ", ".join(details["mute"][:3])
        repair = "Print one line per assertion, or declare the gap with [SKIP]."
    elif details.get("coverage_dropped") or details.get("vanished"):
        headline = "tooling test COVERAGE fell since the last green run"
        repair = 'Restore it, or lower the mark on the record: accept --reason "..."'
    elif details.get("locked"):
        headline = "a tooling suite could not be MEASURED - a document is held open (%s)" \
                   % ", ".join(Path(p).name for p in details["locked_files"][:3])
        repair = "Close that document and re-run. The code is not the problem."
    elif details.get("missing_lib"):
        headline = "a tooling suite could not be MEASURED - this interpreter lacks %s" \
                   % ", ".join(details["missing_modules"][:3])
        repair = "Patch the interpreter shown above. Do NOT edit the code."
    else:
        headline = "a tooling test suite is RED"
        repair = "Fix it before stopping - or say explicitly that you are leaving it red."
    print("\n".join([
        "",
        "TOOLING GATE: %s, after %s." % (headline, why),
        repair,
        'Re-run: python "%s" run' % __file__,
        "(emergency: KB_TOOLING_GATE_OFF=1 disables this gate)",
    ]), file=sys.stderr)
    if again:
        print("Already blocked once this turn - allowing the stop so nothing gets trapped.",
              file=sys.stderr)
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Gate that runs the tests guarding vault tooling.")
    ap.add_argument("cmd", choices=["run", "list", "hook-stop", "accept", "forget"])
    ap.add_argument("--if-stale", action="store_true",
                    help="run: only when a tooling file changed since the last green run")
    ap.add_argument("--reason", default="",
                    help="accept: why the coverage mark is being lowered (required)")
    ap.add_argument("--vault", default="", help="vault root (default: nearest marker dir)")
    ap.add_argument("--state", default="", help="green-marker file (default: per-machine cache)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per suite")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook-stop":
        try:
            return cmd_hook_stop(args)
        except Exception as e:                   # noqa: BLE001 - fail open, never trap a session
            print("tooling_selfcheck hook error (allowing): %r" % (e,), file=sys.stderr)
            return 0

    # --vault is VALIDATED, not merely accepted: a typo would find no suites, report green
    # and write a green marker - after which the gate stays silent forever. A gate whose
    # failure mode is "quietly reassuring" is worse than no gate.
    vault = Path(args.vault).resolve() if args.vault else find_vault(Path(__file__).resolve())
    if not is_vault(vault):
        print("error: %s is not a vault root (looked for %s) - check --vault"
              % (vault or Path(__file__).parent, ", ".join(ROOT_MARKERS)), file=sys.stderr)
        return 2
    if args.cmd == "run":
        return cmd_run(args, vault)
    if args.cmd == "accept":
        # An acceptance with no stated reason is exactly the quiet lowering this exists to
        # prevent, so the empty case is refused rather than defaulted.
        if not args.reason.strip():
            print("error: accept requires --reason \"why coverage is allowed to fall\"",
                  file=sys.stderr)
            return 2
        return cmd_run(args, vault)
    if args.cmd == "list":
        return cmd_list(args, vault)
    return cmd_forget(args, vault)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
