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
fingerprint of the last GREEN run. That marker is a per-machine cache kept OUTSIDE the
vault: two machines edit at different times, and a marker inside a synced folder is one
more file for the sync to conflict over. Losing the cache costs one redundant run and can
never produce a wrong answer.

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

USAGE
    tooling_selfcheck.py run [--if-stale]     # run everything; exit 1 if any suite fails
    tooling_selfcheck.py list                 # suites found + tools with no test beside them
    tooling_selfcheck.py hook-stop            # for the Stop hook (payload on stdin)
    tooling_selfcheck.py forget               # drop the green marker, forcing a full run

Flags: --vault PATH - --state PATH - --timeout SECONDS (default 300, per suite) - --json
Kill switch: KB_TOOLING_GATE_OFF=1 (the hook allows everything; the runner still works).

Exit: 0 = green / nothing to do - 1 = a suite FAILED - 2 = HOOK BLOCKED the turn.
Python 3.8+, standard library only.
"""

from __future__ import annotations

import argparse
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


def state_path(explicit: str = "") -> Path:
    """Per-machine cache, outside the vault (see module docstring)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("KB_TOOLING_STATE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "kb-tooling-selfcheck" / ("state-%s.json" % HOST)


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
    try:
        # -B: do not litter the vault with __pycache__ nobody will ever clean up.
        r = subprocess.run([sys.executable, "-B", str(test)], capture_output=True,
                           cwd=str(test.parent), timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = "exceeded %ds - counted as FAILED" % timeout, 124
    except OSError as e:
        out, code = "could not run: %r" % (e,), 125
    return {"test": test.name, "path": str(test), "ok": code == 0, "code": code,
            "seconds": round(time.perf_counter() - t0, 1), "output": out}


def run_all(vault: Path, timeout: int, quiet: bool = False):
    results = []
    for t in discover(vault):
        res = run_one(t, timeout)
        results.append(res)
        if not quiet:
            print("%s %-28s (%.1fs)  %s" % ("PASS" if res["ok"] else "FAIL", res["test"],
                                            res["seconds"], res["path"]))
            if not res["ok"]:
                for line in res["output"].strip().splitlines()[-12:]:
                    print("      | " + line)
    return results


def cmd_run(args, vault: Path) -> int:
    sp = state_path(args.state)
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
        results = run_all(vault, args.timeout, quiet=args.json)
        bad = [r for r in results if not r["ok"]]
        if not results:
            if not args.json:
                print("No suites found (%s/**/attachments/test_*.py)." % vault.name)
            save_state(sp, {"last_ok": time.time(), "host": HOST, "tests": 0,
                            "signature": signature(vault)})
            return 0
        if not bad:
            save_state(sp, {"last_ok": time.time(), "host": HOST, "tests": len(results),
                            "signature": signature(vault)})
        if args.json:
            print(json.dumps({"ok": not bad, "seconds": round(time.perf_counter() - t0, 1),
                              "results": [{k: v for k, v in r.items() if k != "output"}
                                          for r in results]}, ensure_ascii=False, indent=1))
        else:
            print("\nRESULT (%.1fs): %s" % (
                time.perf_counter() - t0,
                "ALL PASS, %d suites" % len(results) if not bad
                else "FAILED %d/%d - %s" % (len(bad), len(results),
                                            ", ".join(r["test"] for r in bad))))
        return 1 if bad else 0
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
    need, why = stale(vault, state_path(args.state))
    print("\nGreen marker: %s (%s)" % ("needs a run" if need else "still valid", why))
    return 0


def cmd_forget(args, vault: Path) -> int:
    sp = state_path(args.state)
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
    sp = state_path(args.state)
    need, why = stale(vault, sp)
    if not need:
        return 0                                 # the quiet path: ~0.2s per turn

    ns = argparse.Namespace(state=args.state, if_stale=False, json=False, timeout=args.timeout)
    if cmd_run(ns, vault) == 0:
        return 0
    again = bool(d.get("stop_hook_active"))
    print("\n".join([
        "",
        "TOOLING GATE: a tooling test suite is RED after %s." % why,
        "Fix it before stopping - or say explicitly that you are leaving it red.",
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
    ap.add_argument("cmd", choices=["run", "list", "hook-stop", "forget"])
    ap.add_argument("--if-stale", action="store_true",
                    help="run: only when a tooling file changed since the last green run")
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
    if args.cmd == "list":
        return cmd_list(args, vault)
    return cmd_forget(args, vault)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
