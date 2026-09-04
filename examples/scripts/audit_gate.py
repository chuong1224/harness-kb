#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_gate.py - make "always finish with a clean audit" a rule the runtime enforces.

THE PROBLEM. A knowledge base accumulates checkers: an integrity gate, a rules-drift
checker, a work-registry gate, a test suite. Writing "run all of them before you call the
work done" into the docs is a *social contract*. It holds exactly as long as every agent
session remembers it, and nothing catches the session that doesn't. Checkers nobody runs
rot, and a rotten checker is worse than no checker: it looks like coverage.

THE FIX. Run the gates from a `Stop` hook and refuse to end the turn while something is
broken. Exit 2 blocks the turn; the reason printed on stderr goes back to the model.

THE HARD PART IS NOT BLOCKING. IT IS NOT BLOCKING TOO MUCH.

A naive version compares pass/fail and jails the session for any red lamp - including a
red lamp somebody else lit. That happened on the first day this was written: the integrity
gate went red because a *different* session hours earlier had left a note without
frontmatter. A session working on something entirely unrelated would have been trapped by
an error it did not cause and had no context to fix.

So this gate does not compare pass/fail. It compares the **set of findings** against a
stored baseline, and blocks only on findings that are *new*:

    findings_now - baseline  ->  empty: let the turn end
                                 non-empty: block, and name exactly what is new

Inherited debt in the baseline never blocks, but it is printed on every run so it cannot
rot in silence. When a genuinely unrelated finding does block you, the way out is one
command rather than switching the gate off:

    audit_gate.py accept --why "left by the 16:57 session, tracked as TASK-EXAMPLE"

`--why` is mandatory. Accepting debt without recording why is how a gate dies while still
appearing to be alive.

THREE GUARANTEES AGAINST JAILING A SESSION.
  * `stop_hook_active` in the hook payload means this turn was already blocked once - the
    gate reports and steps aside. Being stuck in a loop is worse than missing one finding.
  * Every ambiguous case fails **open**: no vault found, unreadable payload, a gate whose
    command is missing, an unexpected exception. A broken guard must not imprison anyone.
  * `AUDIT_GATE_OFF=1` turns it off outright.

CONFIGURING YOUR GATES. Gates are declared in JSON, not hardcoded - see
`examples/rules/gates.example.json`. Each entry:

    {"name":  "integrity",                      # used in finding keys, keep it stable
     "cmd":   ["python", "scripts/verify_kb.py", "{vault}"],
     "parse": "json:errors",                    # see PARSERS below
     "paths": ["**/*.md"]}                      # run only if a matching file changed

PARSERS turn a gate's output into finding keys. A key must be stable across runs or
everything looks new forever - strip timestamps and durations.

    json:<field>        parse stdout as JSON, take the list at <field>
    json:checks[].list  the same, flattened out of a list of check objects
    lines:<regex>       every stdout/stderr line matching <regex> (group 1 if present)
    exit                pass/fail only; a non-zero exit becomes a single finding

A gate that exits non-zero but yields no parsed key still produces one finding, so a
checker whose output format drifts can never be silently downgraded to "clean".

SPEED. The quiet path is a fingerprint (mtime+size) of the vault, so a turn that changed
nothing costs a fraction of a second and starts no subprocess. `paths` keeps expensive
suites off the critical path until the files they cover actually change.

The baseline lives per machine **and per vault**, outside the vault (`%LOCALAPPDATA%` /
`~/.cache`): two machines have different rhythms, while two vaults on one machine must
not share accepted findings. The filename includes a stable hash of the canonical vault
root without exposing that path. An explicit `--state` / `AUDIT_GATE_STATE` still means
intentional sharing. Ambiguous hostname-only files are not migrated; each vault establishes
a fresh baseline. Keeping state outside a cloud-synced vault avoids conflict copies, and
losing it costs one redundant run rather than a wrong answer.

    audit_gate.py run [--force]         run the gates; exit 1 if anything is new
    audit_gate.py hook-stop             Stop hook: reads payload on stdin, exit 2 blocks
    audit_gate.py status                baseline, inherited debt, whether a run is due
    audit_gate.py accept --why "..."    adopt current findings as the baseline
    audit_gate.py forget                drop the baseline

Flags: --vault PATH  --config PATH  --state PATH  --timeout SEC  --budget SEC  --json
Exit:  0 fine / only old debt / failed open · 1 new findings (`run`) · 2 hook blocked.
Zero dependencies, standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
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
SKIP_SUFFIXES = (".jsonl", ".lock", ".tmp")
SKIP_CONTAINS = ("conflicted copy", "conflict-copy", ".bak-")

DEFAULT_TIMEOUT = int(os.environ.get("AUDIT_GATE_TIMEOUT") or 120)
# A Stop hook is killed from outside after a hard limit. Stop before that ourselves, so it
# is always this script that gives up - a process killed from outside reports nothing.
HOOK_BUDGET = int(os.environ.get("AUDIT_GATE_BUDGET") or 150)
MIN_SLICE = 5                                    # do not start another gate under this


# ------------------------------------------------------------------ foundations

def find_vault(start: Path, marker: str = ".obsidian"):
    try:
        cands = [start, *start.parents]
    except (OSError, ValueError):
        return None
    for cand in cands:
        try:
            if (cand / marker).is_dir():
                return cand
        except OSError:
            continue
    return None


def _vault_key(vault: Path) -> str:
    """Stable, filesystem-aware namespace without exposing the vault path in a filename."""
    try:
        root = os.path.normcase(str(vault.resolve()))
    except (OSError, RuntimeError):
        root = os.path.normcase(os.path.abspath(os.fspath(vault)))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def state_path(vault: Path, explicit: str = "") -> Path:
    """Per-machine, per-vault cache unless the caller explicitly chooses a shared path."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("AUDIT_GATE_STATE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "audit-gate" / ("state-%s-%s.json" % (HOST, _vault_key(vault)))


def gate_off() -> bool:
    return (os.environ.get("AUDIT_GATE_OFF") or "").strip() not in ("", "0", "false", "no")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_state(path: Path) -> dict:
    d = load_json(path)
    return d if isinstance(d, dict) else {}


def save_state(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass                                     # a broken cache must never break the gate


# ------------------------------------------------------------------ fingerprint

def fingerprint(vault: Path) -> dict:
    sig = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            low = f.lower()
            if low.endswith(SKIP_SUFFIXES) or any(s in low for s in SKIP_CONTAINS):
                continue
            p = Path(root) / f
            try:
                st = p.stat()
            except OSError:
                continue
            sig[str(p.relative_to(vault)).replace("\\", "/")] = [int(st.st_mtime), st.st_size]
    return sig


def changed_paths(cur: dict, old) -> list:
    """Paths whose stamp differs. No previous baseline means *everything* changed: guess
    low and you skip the very gate that would have caught the problem."""
    if not isinstance(old, dict) or not old:
        return sorted(cur)
    return sorted(k for k in set(cur) | set(old) if cur.get(k) != old.get(k))


def gate_is_due(gate: dict, changed: list, first_run: bool) -> bool:
    pats = gate.get("paths") or ["**"]
    if first_run or "**" in pats:
        return True
    for p in changed:
        for pat in pats:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat.replace("**/", "")):
                return True
    return False


# ------------------------------------------------------------------------ lock

def _busy_lock(path: Path):
    """One run at a time per machine. If another holds it, skip the turn rather than
    queue: the baseline is not written, so the next turn still catches everything, while
    queueing inside a Stop hook is the reliable way to eat the whole time limit."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lf = open(str(path) + ".lock", "a+")
    except OSError:
        return None, True
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            for _ in range(50):
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


# --------------------------------------------------------------------- parsers

_DURATION = re.compile(r"\(\d+(?:[.,]\d+)?\s*m?s\)")


def _dig(doc, field: str):
    """`checks[].list` walks into a list of objects and flattens the named lists."""
    cur = doc
    for part in field.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            cur = cur.get(key) if isinstance(cur, dict) else None
            if not isinstance(cur, list):
                return []
            return [it for it in cur]
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def parse_findings(gate: dict, code: int, out: str, err: str) -> list:
    name, how = gate["name"], (gate.get("parse") or "exit")
    keys = []
    if how.startswith("json:"):
        field = how[5:]
        doc = None
        try:
            doc = json.loads(out.encode("utf-8").decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            doc = None
        if doc is None:
            if code:
                return ["%s: exit %d and stdout was not readable JSON" % (name, code)]
            return []
        if field.endswith("[].list") or "[]" in field:
            head = field.split("[]")[0]
            tail = field.split("[]")[-1].lstrip(".")
            for obj in (_dig(doc, head + "[]") or []):
                if isinstance(obj, dict):
                    ident = obj.get("id") or obj.get("name") or ""
                    for item in (obj.get(tail) or []):
                        keys.append("%s/%s: %s" % (name, ident, str(item).strip())
                                    if ident else "%s: %s" % (name, str(item).strip()))
        else:
            for item in (_dig(doc, field) or []):
                keys.append("%s: %s" % (name, str(item).strip()))
    elif how.startswith("lines:"):
        rx = re.compile(how[6:])
        for line in (out + "\n" + err).splitlines():
            m = rx.search(line)
            if m:
                raw = m.group(1) if m.groups() else m.group(0)
                keys.append("%s: %s" % (name, _DURATION.sub("", raw).strip()))
    if code and not keys:
        keys.append("%s: exit %d with no finding it could name" % (name, code))
    return keys


# ------------------------------------------------------------------- executing

# Wired to this repo's own two checkers, and to the shape they actually print - which is
# not the same shape for both. `verify_kb.py` has no --json and prints "  [ERR ] path: msg";
# `check_rules_drift.py` does have --json and puts its blocking findings under "errors".
# Matching each gate to its real output is the whole job of a config: a default that
# assumes one house format would fail on first contact with a real checker.
DEFAULT_CONFIG = {
    "gates": [
        {"name": "integrity", "parse": r"lines:^\s*\[ERR\s*\]\s+(.+)$", "paths": ["**"],
         "cmd": ["{python}", "examples/scripts/verify_kb.py", "{vault}",
                 "--rules", "examples/rules/rules.example.json"]},
        {"name": "rules-drift", "parse": "json:errors", "paths": ["**"],
         "cmd": ["{python}", "examples/scripts/check_rules_drift.py", "{vault}",
                 "--rules", "examples/rules/rules.example.json", "--json"]},
    ]
}


def load_config(vault: Path, explicit: str):
    """Returns (config, base). Relative paths in `cmd` resolve against **base**, the
    directory holding the config - not against the vault and not against the current
    working directory. A gate config describes where the *tools* live, and the tools are
    rarely inside the notes; resolving against the caller's cwd would make the same config
    behave differently depending on where somebody happened to stand."""
    for cand in ([Path(explicit)] if explicit else
                 [vault / "gates.json", vault / ".audit-gate.json"]):
        doc = load_json(cand)
        if isinstance(doc, dict) and doc.get("gates"):
            return doc, cand.resolve().parent
    if explicit:
        raise SystemExit("config not found or has no 'gates': %s" % explicit)
    return DEFAULT_CONFIG, Path(__file__).resolve().parent.parent.parent


def _expand(cmd, vault: Path, base: Path) -> list:
    out = []
    for i, a in enumerate(cmd):
        a = a.replace("{vault}", str(vault)).replace("{python}", sys.executable)
        if i and not os.path.isabs(a) and not a.startswith("-"):
            cand = base / a
            if cand.exists():
                a = str(cand)
        out.append(a)
    return out


def run_gate(gate: dict, vault: Path, base: Path, timeout: int):
    """Capture output directly - never through a shell pipe.

    `checker | tail -3` reports the exit status of `tail`, which almost always succeeds,
    so a failing gate reads as green. Bytes are decoded here as UTF-8 for the same family
    of reason: a shell redirect on Windows may prepend a BOM and decode as the local code
    page, which is enough to break both JSON and any non-ASCII text."""
    try:
        r = subprocess.run(_expand(gate["cmd"], vault, base), capture_output=True,
                           cwd=str(gate.get("cwd") or base), timeout=timeout)
        return (r.returncode,
                (r.stdout or b"").decode("utf-8", "replace"),
                (r.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return 124, "", "did not finish within %ds - treated as failing" % timeout
    except OSError as e:
        return 125, "", "could not run: %r" % (e,)


def run_gates(cfg: dict, base: Path, vault: Path, changed: list, first_run: bool,
              timeout: int, budget: int, quiet: bool):
    keys, skipped, not_run = [], [], []
    started = time.time()
    for gate in cfg.get("gates") or []:
        name = gate.get("name") or "?"
        if not gate_is_due(gate, changed, first_run):
            skipped.append(name)
            continue
        argv = _expand(gate.get("cmd") or [], vault, base)
        # A configured gate whose command is absent is skipped, not failed: the same
        # config is meant to travel between machines that do not all install everything.
        if len(argv) < 2 or not Path(argv[1]).exists():
            skipped.append(name)
            continue
        left = (budget - (time.time() - started)) if budget else None
        if left is not None and left < MIN_SLICE:
            not_run.append(name)
            continue
        limit = int(min(timeout, left)) if left is not None else timeout
        t0 = time.time()
        code, out, err = run_gate(gate, vault, base, limit)
        found = parse_findings(gate, code, out, err)
        keys += found
        if not quiet:
            print("  %-14s exit=%-4s %.1fs  %d finding(s)"
                  % (name, code, time.time() - t0, len(found)))
    return sorted(set(keys)), skipped, not_run


# --------------------------------------------------------------------- commands

def evaluate(args, vault: Path, quiet: bool) -> dict:
    sp = state_path(vault, args.state)
    st = load_state(sp)
    cfg, base = load_config(vault, args.config)
    cur = fingerprint(vault)
    first = not isinstance(st.get("signature"), dict) or not st.get("signature")
    changed = changed_paths(cur, st.get("signature"))
    force = bool(getattr(args, "force", False))
    res = {"skipped_run": "", "new": [], "old": [], "not_run": [], "skipped": [],
           "seconds": 0.0, "changed": len(changed)}

    if not changed and not force:
        res["skipped_run"] = "nothing changed since %s" % (
            time.strftime("%d %b %H:%M", time.localtime(st["last_run"]))
            if st.get("last_run") else "the last run")
        return res

    lf, got = _busy_lock(sp)
    if not got:
        res["skipped_run"] = "another run holds the lock on this machine"
        return res
    try:
        t0 = time.time()
        keys, skipped, not_run = run_gates(cfg, base, vault, changed, first or force,
                                           args.timeout, getattr(args, "budget", 0) or 0,
                                           quiet)
        known = set(st.get("known") or [])
        new = [k for k in keys if k not in known]
        old = [k for k in keys if k in known]
        res.update({"new": new, "old": old, "not_run": not_run, "skipped": skipped,
                    "seconds": round(time.time() - t0, 1)})
        # Write the baseline only after a complete, clean run. Recording it while blocking
        # would grandfather the very finding just blocked on; recording it after a partial
        # run would silence the gate for a finding nobody has looked at yet.
        if not new and not not_run:
            st.update({"last_run": time.time(), "host": HOST, "signature": cur, "known": old})
            if not old:
                st.pop("accepted_why", None)
                st.pop("accepted_at", None)
            save_state(sp, st)
        return res
    finally:
        _unlock(lf)


def cmd_run(args, vault: Path) -> int:
    res = evaluate(args, vault, quiet=args.json)
    if args.json:
        print(json.dumps(dict(res, ok=not res["new"] and not res["not_run"]),
                         ensure_ascii=False, indent=1))
        return 1 if (res["new"] or res["not_run"]) else 0
    if res["skipped_run"]:
        print("Skipped: %s." % res["skipped_run"])
        return 0
    print("\n%.1fs · %d new · %d inherited%s%s"
          % (res["seconds"], len(res["new"]), len(res["old"]),
             "" if not res["not_run"] else " · NOT RUN: " + ", ".join(res["not_run"]),
             "" if not res["skipped"] else " · skipped: " + ", ".join(res["skipped"])))
    for k in res["new"]:
        print("  NEW  %s" % k)
    for k in res["old"]:
        print("  old  %s" % k)
    if res["old"]:
        print("\nInherited findings are not blocking you, but they are still findings.")
        print("Track them somewhere durable, or they quietly become permanent.")
    return 1 if (res["new"] or res["not_run"]) else 0


def read_payload() -> dict:
    """Read bytes and decode UTF-8 explicitly: a child process on Windows picks stdin's
    encoding from the locale, so text mode mangles any non-ASCII payload."""
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
    if gate_off():
        return 0
    d = read_payload()
    vault = Path(args.vault) if args.vault else find_vault(Path(d.get("cwd") or os.getcwd()))
    if not vault:
        return 0                                 # fail open: no vault, no opinion
    ns = argparse.Namespace(state=args.state, config=args.config, json=False, force=False,
                            timeout=args.timeout, budget=args.budget or HOOK_BUDGET)
    res = evaluate(ns, vault, quiet=True)
    if not res["new"] and not res["not_run"]:
        return 0

    again = bool(d.get("stop_hook_active"))
    lines = ["", "AUDIT GATE: the audit has not converged - this work is not finished."]
    if res["new"]:
        lines += ["", "%d finding(s) that were not in the baseline:" % len(res["new"])]
        lines += ["   * %s" % k for k in res["new"][:12]]
        if len(res["new"]) > 12:
            lines.append("   ... and %d more" % (len(res["new"]) - 12))
    if res["not_run"]:
        lines += ["", "NOT RUN (out of the %ds budget): %s" % (ns.budget,
                                                               ", ".join(res["not_run"])),
                  '   finish them by hand: python "%s" run --force' % __file__]
    if res["old"]:
        lines += ["", "(%d inherited finding(s) are being tolerated and are not what is "
                      "blocking you)" % len(res["old"])]
    lines += [
        "",
        "Fix them, then stop. If a finding was not caused by this turn - another session,",
        "an editor, a sync - adopt it with a reason instead of switching the gate off:",
        '   python "%s" accept --why "..."' % __file__,
        "(emergency: AUDIT_GATE_OFF=1)",
    ]
    if again:
        lines += ["", "Already blocked once this turn - stepping aside so nothing loops."]
    print("\n".join(lines), file=sys.stderr)
    return 0 if again else 2


def cmd_status(args, vault: Path) -> int:
    sp = state_path(vault, args.state)
    st = load_state(sp)
    cfg, _base = load_config(vault, args.config)
    changed = changed_paths(fingerprint(vault), st.get("signature"))
    known = st.get("known") or []
    if args.json:
        print(json.dumps({"state": str(sp), "last_run": st.get("last_run"),
                          "known": known, "changed": len(changed),
                          "gates": [g.get("name") for g in cfg.get("gates") or []],
                          "accepted_why": st.get("accepted_why", ""),
                          "gate_off": gate_off()}, ensure_ascii=False, indent=1))
        return 0
    print("Baseline : %s" % sp)
    print("Last run : %s" % (time.strftime("%d %b %H:%M", time.localtime(st["last_run"]))
                             if st.get("last_run") else "never"))
    print("Gates    : %s" % ", ".join(g.get("name", "?") for g in cfg.get("gates") or []))
    print("Changed  : %d file(s) since that run" % len(changed))
    print("Gate     : %s" % ("OFF (AUDIT_GATE_OFF)" if gate_off() else "on"))
    print("\nInherited findings (%d):" % len(known))
    for k in known:
        print("   * %s" % k)
    if st.get("accepted_why"):
        print("\nAccepted %s: %s"
              % (time.strftime("%d %b %H:%M", time.localtime(st.get("accepted_at") or 0)),
                 st["accepted_why"]))
    return 0


def cmd_accept(args, vault: Path) -> int:
    if not (args.why or "").strip():
        print('ERROR: --why "reason" is required. Adopting findings without recording why '
              'is how a gate dies while still looking alive.', file=sys.stderr)
        return 2
    sp = state_path(vault, args.state)
    st = load_state(sp)
    cfg, base = load_config(vault, args.config)
    keys, _, not_run = run_gates(cfg, base, vault, [], True, args.timeout, 0, quiet=args.json)
    if not_run:
        print("ERROR: %s did not run - refusing to adopt a partial picture."
              % ", ".join(not_run), file=sys.stderr)
        return 2
    st.update({"known": keys, "signature": fingerprint(vault), "last_run": time.time(),
               "host": HOST, "accepted_why": args.why.strip(), "accepted_at": time.time()})
    save_state(sp, st)
    print("Adopted %d finding(s) as the baseline. Reason: %s" % (len(keys), args.why.strip()))
    for k in keys:
        print("   * %s" % k)
    if keys:
        print("\nThe baseline silences the gate. It does not fix anything - track these.")
    return 0


def cmd_forget(args, vault: Path) -> int:
    sp = state_path(vault, args.state)
    try:
        sp.unlink()
        print("Baseline dropped: %s" % sp)
    except OSError:
        print("No baseline to drop (%s)." % sp)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Block the end of a turn while an audit is unclean.")
    ap.add_argument("cmd", choices=["run", "hook-stop", "status", "accept", "forget"])
    ap.add_argument("--vault", default="", help="vault root (default: nearest .obsidian)")
    ap.add_argument("--config", default="", help="gate config JSON (default: <vault>/gates.json)")
    ap.add_argument("--state", default="", help="baseline file (default: per-machine cache)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per gate")
    ap.add_argument("--budget", type=int, default=0,
                    help="seconds for the whole run (0 = unlimited; hook uses %ds)" % HOOK_BUDGET)
    ap.add_argument("--force", action="store_true", help="run even if nothing changed")
    ap.add_argument("--why", default="", help="accept: why this debt is being adopted")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook-stop":
        try:
            return cmd_hook_stop(args)
        except Exception as e:                   # noqa: BLE001 - fail open, never jail a session
            print("audit_gate hook failed (letting the turn end): %r" % (e,), file=sys.stderr)
            return 0

    # Validate --vault rather than trusting it. A typo makes every gate "missing", which
    # reads as clean, writes a false baseline, and silences the gate for good.
    vault = Path(args.vault).resolve() if args.vault else find_vault(Path.cwd())
    if not vault or not vault.is_dir():
        print("ERROR: no vault found at %s - pass --vault PATH"
              % (vault or Path.cwd()), file=sys.stderr)
        return 2
    if args.cmd == "run":
        return cmd_run(args, vault)
    if args.cmd == "status":
        return cmd_status(args, vault)
    if args.cmd == "accept":
        return cmd_accept(args, vault)
    return cmd_forget(args, vault)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
