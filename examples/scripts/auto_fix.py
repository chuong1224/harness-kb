#!/usr/bin/env python3
"""
auto_fix.py - Close the loop on ONE class of mechanical error (H2).

check_rules_drift.py (H1) made count drift impossible to hide: it reports "this document
says 14, the source of truth says 15" with an exact file:line. But reporting is where most
knowledge bases stop, and the fix goes back into a human queue - so every morning the same
report waits for someone to retype a number a machine already knows.

This script closes that loop for the narrowest class worth automating: a NUMBER on a line
that carries a marker comment. Nothing here needs to understand the sentence around it -
the marker names the claim, the rules file supplies the value, and exactly one token
changes. Everything that needs judgment stays in the report (see "What it refuses to fix").

Guardrails, in the order they run (docs/blueprint.md section 7):

  1. **Two sources must agree.** The checker reports X -> Y, and this script independently
     re-reads that line and finds token X there using the claim's own pattern. If they
     disagree - line edited since, number spelled out in words, marker removed - the fix is
     SKIPPED with a reason. There is no branch that guesses.
  2. **Back up before writing.** Every file about to change is copied, with its hash, into a
     timestamped backup directory plus a manifest; the last 30 runs are kept.
  3. **Optional per-file lock.** If claim.py (H4) sits next to this script, a file held by
     another live stream defers the whole run instead of racing it (exit 3).
  4. **Gate afterwards, roll back on red.** Re-run the drift check (must be clean) and
     verify_kb.py (must exit 0). Either one red = restore every file from the backup and
     exit 1. A "fix" that breaks the gate is not a fix.

What it refuses to fix, deliberately:

  * an incomplete enumeration (which item, inserted where, worded how - that is writing)
  * a marker that was removed (a machine cannot tell "removed on purpose" from "lost")
  * anything semantic: translations, tag classification, note structure, merging notes

Those stay in the audit report for a supervised session. Widening this list is a decision to
make deliberately, not a flag to flip.

Exit code 0 = applied or nothing to do, 1 = rolled back / rollback problem, 2 = usage error,
3 = deferred because another stream holds a target file.
Zero dependencies: Python 3.8+, standard library only.

Usage:
    python auto_fix.py /path/to/vault --rules rules.example.json            # dry run
    python auto_fix.py /path/to/vault --rules rules.example.json --apply
    python auto_fix.py /path/to/vault --rules rules.example.json --rollback last
    python auto_fix.py /path/to/vault --rules rules.example.json --history
    (add --json anywhere for machine-readable output)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER_RE = re.compile(r"<!--\s*rules:([a-z_]+)\s*-->")
# The text contract with check_rules_drift.py. If that message ever changes, the
# "drift is fixed" case in test_auto_fix.py goes red immediately - which is the point.
DRIFT_RE = re.compile(
    r"^(?P<rel>.+?):(?P<line>\d+): DRIFT '(?P<claim>[a-z_]+)' - "
    r"document says (?P<got>\d+), source of truth says (?P<want>\d+)"
)
KEEP_BACKUPS = 30


def die(msg, code=2):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def backup_root(explicit=""):
    if explicit:
        return Path(explicit)
    env = os.environ.get("KB_AUTOFIX_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "kb-autofix"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd, cwd):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(cwd))


def read_lines(path):
    """Read RAW so the file's own line endings survive the edit.

    Python's default text read turns CRLF into '\\n' on the way in; write it back and a
    one-token fix silently rewrites every line of the file. On a synced vault that also
    looks like "the whole document changed" to every other machine. Notes written on
    Windows are CRLF often enough that this is a certainty, not a corner case.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    return text.splitlines(keepends=True)


def drift_check(vault, rules, as_json=True):
    tool = HERE / "check_rules_drift.py"
    if not tool.exists():
        die("check_rules_drift.py must sit next to auto_fix.py (it is the sensor)")
    cmd = [sys.executable, tool, vault, "--rules", rules]
    if as_json:
        cmd.append("--json")
    p = run(cmd, vault)
    if not as_json:
        return {"_exit": p.returncode, "_out": p.stdout}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        die("check_rules_drift.py did not return readable JSON:\n" + (p.stdout or p.stderr)[:400])
    data["_exit"] = p.returncode
    return data


def integrity_gate(vault, rules):
    """verify_kb.py is the note-level gate; absent is fine, it just means one less net."""
    tool = HERE / "verify_kb.py"
    if not tool.exists():
        return {"skipped": True, "exit": 0}
    p = run([sys.executable, tool, vault, "--rules", rules], vault)
    return {"skipped": False, "exit": p.returncode, "tail": (p.stdout or "").strip()[-300:]}


def mask(line):
    """Blank markers/emphasis/backticks with SAME-LENGTH spaces.

    The checker strips those characters before matching, which is fine for detection but
    shifts every offset - and writing a fix needs the offset on the original line to be
    exact. Same-length masking keeps every character in place.
    """
    out = MARKER_RE.sub(lambda m: " " * len(m.group(0)), line)
    return out.replace("**", "  ").replace("`", " ")


def owned_window(line, claim, claim_types):
    """(lo, hi) slice of the line that `claim` owns, or None meaning the whole line.

    The twin of check_rules_drift.marker_segments: the checker now reads each number
    inside the segment belonging to its own marker, so the fixer has to look for the
    token in that same segment. Without this, a line carrying two markers whose numbers
    happen to be EQUAL sends finditer to the first occurrence - the other marker's
    number - and the fix lands on the wrong token: the correct number becomes wrong and
    the wrong one survives. drop_ambiguous cannot catch that one, because a single fix
    is planned and nothing overlaps.
    """
    occurrences = [(m.group(1), m.start(), m.end()) for m in MARKER_RE.finditer(line)]
    numeric = [c for c, _s, _e in occurrences
               if (claim_types.get(c) or {}).get("kind") != "list"]
    if len(numeric) <= 1:
        return None
    previous_end = 0
    for name, start, end in occurrences:
        if name == claim and (claim_types.get(name) or {}).get("kind") != "list":
            return (previous_end, start)
        previous_end = end
    return None


def locate(line, patterns, got, window=None):
    """Span of the number token to rewrite, or None when we are not certain."""
    masked = mask(line)
    lo, hi = window if window else (0, len(masked))
    for pat in patterns:
        for m in re.finditer(pat, masked[lo:hi]):
            if m.group(1) == str(got):
                return (lo + m.start(1), lo + m.end(1))
    return None


def claims_acquire(vault, rels, stream):
    """HOLD the lock on every target before writing (H4), not merely ask about it.

    Checking and then writing leaves a window: the answer is already stale by the time the
    write lands. Shell writes do not pass through the hook, so nothing else is holding the
    file on this script's behalf. If any file cannot be taken, release the ones already
    taken and defer the whole run - a half-applied batch is worse than a deferred one.
    """
    tool = HERE / "claim.py"
    if not tool.exists():
        return [], False
    taken, blocked = [], []
    for rel in rels:
        p = run([sys.executable, tool, "take", rel, "--vault", vault, "--stream", stream,
                 "--why", "auto-fix of marked count claims"], vault)
        (taken if p.returncode == 0 else blocked).append(rel)
    if blocked and taken:
        claims_release(vault, stream)
    return blocked, bool(taken)


def claims_release(vault, stream):
    tool = HERE / "claim.py"
    if tool.exists():
        run([sys.executable, tool, "release", "--all", "--vault", vault, "--stream", stream], vault)


def plan(vault, rules_path, rules, report):
    claim_types = rules.get("documents", {}).get("claim_types", {})
    patterns_of = {k: v.get("patterns", []) for k, v in claim_types.items()}
    fixes, skipped = [], []
    for err in report.get("errors", []):
        m = DRIFT_RE.match(err)
        if not m:
            skipped.append({"why": "not an auto-fixable class", "detail": err})
            continue
        rel, lineno = m.group("rel"), int(m.group("line"))
        got, want, claim = int(m.group("got")), int(m.group("want")), m.group("claim")
        path = vault / rel
        if not path.exists():
            skipped.append({"why": "file is gone", "detail": err})
            continue
        lines = read_lines(path)
        if lineno > len(lines):
            skipped.append({"why": "line number past end of file", "detail": err})
            continue
        line = lines[lineno - 1]
        if not MARKER_RE.search(line):
            skipped.append({"why": "line no longer carries a marker", "detail": err})
            continue
        span = locate(line, patterns_of.get(claim, []), got,
                      owned_window(line, claim, claim_types))
        if span is None:
            skipped.append({"why": "could not locate the number on that line", "detail": err})
            continue
        fixes.append({"file": rel, "line": lineno, "claim": claim, "got": got, "want": want,
                      "col": span[0], "end": span[1],
                      "before": line.rstrip("\r\n")[:120],
                      "after": (line[:span[0]] + str(want) + line[span[1]:]).rstrip("\r\n")[:120]})
    return drop_ambiguous(fixes, skipped)


def drop_ambiguous(fixes, skipped):
    """Two fixes aiming at the SAME number token: skip both rather than pick one.

    This used to happen whenever a line carried two markers of the same unit (say a total
    and a subset, both written as "N tags"): the checker scanned patterns across the whole
    line, handed BOTH claims the leftmost number, and the two fixes landed on one column
    with two different values.

    That is fixed at the sensor now - each marker owns the text before it, and owned_window
    keeps the fixer inside the same boundary - so two legitimate fixes can no longer overlap
    and this guard should never fire in practice. It stays as a last-resort invariant: if
    someone widens claim_types.patterns until two segments bleed into each other, the run
    must REFUSE rather than overwrite a number it cannot attribute. Fixing one and rolling
    the batch back would also throw away the legitimate fixes made in other files.
    """
    by_slot = {}
    for f in fixes:
        by_slot.setdefault((f["file"], f["line"]), []).append(f)
    kept = []
    for (rel, lineno), items in by_slot.items():
        clash = [f for f in items if any(
            g is not f and f["col"] < g["end"] and g["col"] < f["end"] for g in items)]
        if clash:
            claims = ", ".join(sorted("%s->%s" % (f["claim"], f["want"]) for f in clash))
            skipped.append({
                "why": "several claims point at the same number on this line",
                "detail": "%s:%d: %s - the line carries more than one marker of the same "
                          "unit; the checker cannot tell which number belongs to which "
                          "marker. Split them onto separate lines." % (rel, lineno, claims)})
            kept += [f for f in items if f not in clash]
        else:
            kept += items
    return kept, skipped


def do_backup(vault, rels, root):
    bid = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / bid / "files"
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i, rel in enumerate(rels):
        src = vault / rel
        dst = d / ("%03d-%s" % (i, Path(rel).name))
        shutil.copy2(src, dst)
        files.append({"rel": rel, "backup": dst.name, "sha_before": sha256(src)})
    manifest = {"id": bid, "vault": str(vault), "files": files,
                "created_at": datetime.now().isoformat(timespec="seconds")}
    (root / bid / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for old in sorted([p for p in root.iterdir() if p.is_dir()])[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return manifest


def restore(manifest, vault, root, force=False):
    done, problems = [], []
    for ent in manifest["files"]:
        src = root / manifest["id"] / "files" / ent["backup"]
        target = vault / ent["rel"]
        if not src.exists():
            problems.append("%s: backup copy missing" % ent["rel"])
            continue
        if not force and "sha_after" in ent and target.exists() \
                and sha256(target) != ent["sha_after"]:
            problems.append("%s: changed again after the fix (use --force to overwrite)" % ent["rel"])
            continue
        shutil.copy2(src, target)
        done.append(ent["rel"])
    return done, problems


def log_history(root, rec):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "history.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def emit(as_json, payload, human):
    print(json.dumps(payload, ensure_ascii=False, indent=2) if as_json else human)


def cmd_apply(args, vault, rules_path, rules, root):
    report = drift_check(vault, rules_path)
    fixes, skipped = plan(vault, rules_path, rules, report)
    if not fixes:
        left = len(report.get("errors", []))
        emit(args.json, {"ok": True, "applied": [], "skipped": skipped, "unfixable": left},
             "nothing to fix" if not left else
             "nothing auto-fixable - %d drift error(s) remain, all outside the safe class" % left)
        log_history(root, {"ts": datetime.now().isoformat(timespec="seconds"),
                           "result": "noop", "applied": 0})
        return 0

    rels = sorted({f["file"] for f in fixes})
    # A dedicated stream id on purpose: this run ends with "release --all", and borrowing
    # the calling session's stream would drop claims that session holds for other work.
    stream = os.environ.get("KB_AUTOFIX_STREAM") or ("autofix-%d" % os.getpid())
    blocked, holding = claims_acquire(vault, rels, stream)
    if blocked:
        emit(args.json, {"ok": False, "deferred": blocked},
             "deferred - another stream holds: " + ", ".join(blocked))
        return 3
    try:
        return _apply_locked(args, vault, rules_path, rules, root, fixes, skipped)
    finally:
        if holding:
            claims_release(vault, stream)


def _apply_locked(args, vault, rules_path, rules, root, fixes, skipped):
    """The writing half - runs while every target file is held; the caller releases."""
    rels = sorted({f["file"] for f in fixes})
    manifest = do_backup(vault, rels, root)
    applied, errors = [], []
    # Write per FILE, and within a line replace RIGHT to LEFT using the columns recorded
    # at planning time. Re-locating after each write is what bites you when one line
    # carries two markers whose values collide: claim A becomes 15, and the search for
    # claim B's current value of 15 then lands on the number just written. Fixed columns
    # plus a token check at that exact offset removes the whole class - and a line edited
    # by someone else in the meantime fails the check instead of being mangled.
    by_file = {}
    for f in fixes:
        by_file.setdefault(f["file"], []).append(f)
    for rel, items in by_file.items():
        path = vault / rel
        lines = read_lines(path)
        done_here = []
        for f in sorted(items, key=lambda x: (-x["line"], -x["col"])):
            line = lines[f["line"] - 1]
            col, token = f["col"], str(f["got"])
            if line[col:col + len(token)] != token:
                errors.append("%s:%d: line changed between planning and writing" % (rel, f["line"]))
                continue
            lines[f["line"] - 1] = line[:col] + str(f["want"]) + line[col + len(token):]
            done_here.append(f)
        if done_here:
            path.write_text("".join(lines), encoding="utf-8", newline="")
            applied.extend(done_here)

    verify = verify_after(vault, rules_path, errors)
    for ent in manifest["files"]:
        t = vault / ent["rel"]
        if t.exists():
            ent["sha_after"] = sha256(t)
    (root / manifest["id"] / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    if not verify["ok"]:
        done, problems = restore(manifest, vault, root, force=True)
        log_history(root, {"ts": datetime.now().isoformat(timespec="seconds"),
                           "result": "rolled_back", "backup_id": manifest["id"],
                           "why": verify["why"]})
        emit(args.json, {"ok": False, "rolled_back": True, "backup_id": manifest["id"],
                         "why": verify["why"], "restored": done, "problems": problems},
             "gate red after the fix - rolled back %d file(s)\n  reason: %s\n  backup: %s"
             % (len(done), verify["why"], manifest["id"]))
        return 1

    log_history(root, {"ts": datetime.now().isoformat(timespec="seconds"), "result": "applied",
                       "backup_id": manifest["id"], "applied": len(applied),
                       "files": [f["file"] for f in applied]})
    lines_out = ["fixed %d claim(s), gate green, backup %s" % (len(applied), manifest["id"])]
    lines_out += ["  %s:%d [%s] %s -> %s" % (f["file"], f["line"], f["claim"], f["got"], f["want"])
                  for f in applied]
    lines_out += ["  skipped (%s): %s" % (s["why"], s["detail"][:100]) for s in skipped]
    lines_out.append("  rollback: python auto_fix.py <vault> --rules <rules> --rollback %s"
                     % manifest["id"])
    emit(args.json, {"ok": True, "applied": applied, "skipped": skipped,
                     "backup_id": manifest["id"], "verify": verify}, "\n".join(lines_out))
    return 0


def verify_after(vault, rules_path, errors):
    if os.environ.get("KB_AUTOFIX_FORCE_VERIFY_FAIL") == "1":     # tests only
        return {"ok": False, "why": "forced failure via KB_AUTOFIX_FORCE_VERIFY_FAIL"}
    if errors:
        return {"ok": False, "why": "; ".join(errors)[:300]}
    after = drift_check(vault, rules_path)
    if after.get("errors"):
        return {"ok": False, "why": "drift check still reports %d error(s)" % len(after["errors"])}
    gate = integrity_gate(vault, rules_path)
    if gate["exit"] != 0:
        return {"ok": False, "why": "verify_kb.py exited %d after the fix" % gate["exit"]}
    return {"ok": True, "why": "drift check clean and integrity gate green", "gate": gate}


def cmd_plan(args, vault, rules_path, rules, root):
    report = drift_check(vault, rules_path)
    fixes, skipped = plan(vault, rules_path, rules, report)
    payload = {"ok": True, "mode": "dry-run", "fixes": fixes, "skipped": skipped,
               "drift_errors": len(report.get("errors", []))}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not fixes:
        print("nothing to fix (%d drift error(s) reported)" % len(report.get("errors", [])))
    for f in fixes:
        print("  %s:%d [%s] %s -> %s" % (f["file"], f["line"], f["claim"], f["got"], f["want"]))
        print("      before: " + f["before"])
        print("      after : " + f["after"])
    for s in skipped:
        print("  skipped (%s): %s" % (s["why"], s["detail"][:110]))
    print("\ndry run - nothing written. Add --apply to write.")
    return 0


def cmd_rollback(args, vault, rules_path, rules, root):
    dirs = sorted([d for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []
    if not dirs:
        emit(args.json, {"ok": False, "why": "no runs recorded"}, "no runs to roll back")
        return 1
    chosen = dirs[-1] if args.rollback == "last" else next(
        (d for d in dirs if d.name == args.rollback), None)
    if chosen is None:
        emit(args.json, {"ok": False, "why": "unknown backup id"},
             "unknown backup id; available: " + ", ".join(d.name for d in dirs[-5:]))
        return 1
    manifest = json.loads((chosen / "manifest.json").read_text(encoding="utf-8"))
    done, problems = restore(manifest, vault, root, force=args.force)
    log_history(root, {"ts": datetime.now().isoformat(timespec="seconds"), "result": "rollback",
                       "backup_id": manifest["id"], "restored": len(done), "problems": problems})
    emit(args.json, {"ok": not problems, "restored": done, "problems": problems},
         "restored %d file(s) from %s" % (len(done), manifest["id"])
         + ("" if not problems else "\n  " + "\n  ".join(problems)))
    return 1 if problems else 0


def cmd_history(args, vault, rules_path, rules, root):
    p = root / "history.jsonl"
    if not p.exists():
        emit(args.json, {"ok": True, "records": []}, "no history yet")
        return 0
    recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.json:
        print(json.dumps(recs[-20:], ensure_ascii=False, indent=2))
        return 0
    for r in recs[-20:]:
        print("%s  %-12s %s changed  backup %s" % (r.get("ts", "?"), r.get("result", "?"),
                                                   r.get("applied", r.get("restored", 0)),
                                                   r.get("backup_id", "-")))
    return 0


def main():
    ap = argparse.ArgumentParser(description="H2 - auto-fix the safe class of rule drift")
    ap.add_argument("vault", help="path to the vault (folder of Markdown notes)")
    ap.add_argument("--rules", required=True, help="path to the rules JSON (single source of truth)")
    ap.add_argument("--apply", action="store_true", help="write the fixes (default: dry run)")
    ap.add_argument("--rollback", metavar="ID|last", help="restore the files of a previous run")
    ap.add_argument("--history", action="store_true", help="show the last 20 runs")
    ap.add_argument("--backup-dir", default="", help="where backups live (default: user cache dir)")
    ap.add_argument("--force", action="store_true", help="rollback even if files changed since")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        die("vault not found: %s" % vault)
    rules_path = Path(args.rules).resolve()
    if not rules_path.exists():
        die("rules file not found: %s" % rules_path)
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    root = backup_root(args.backup_dir)

    if args.history:
        return cmd_history(args, vault, rules_path, rules, root)
    if args.rollback:
        return cmd_rollback(args, vault, rules_path, rules, root)
    if args.apply:
        return cmd_apply(args, vault, rules_path, rules, root)
    return cmd_plan(args, vault, rules_path, rules, root)


if __name__ == "__main__":
    sys.exit(main())
