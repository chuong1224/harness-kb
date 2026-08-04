#!/usr/bin/env python3
"""
test_auto_fix.py - break-the-fixer tests for auto_fix.py (H2).

An auto-fixer is only worth having if you can prove the guardrails, not the happy path.
Every case below runs against a THROWAWAY COPY of the demo vault, so a failing test can
never damage anything.

  1. a dry run writes nothing at all
  2. a real drift is fixed to exactly the original bytes
  3. running again is a no-op (no empty backup every morning)
  4. a number written in words is SKIPPED, never guessed
  5. a red gate after the fix rolls everything back and exits 1
  6. --rollback restores the state from before a run
  7. files the rules file does not register are never touched
  8. a CRLF file is still CRLF afterwards - a one-token fix must not rewrite every line
  9. two markers sharing a pattern: each number is fixed in its own slot
  9b. ...even when both numbers are already equal, which no overlap check can catch
  9c. the last-resort overlap guard is still alive

Usage: python test_auto_fix.py
Exit:  0 = all pass, 1 = at least one failure
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE.parent / "demo-vault"
RULES_SRC = HERE.parent / "rules" / "rules.example.json"
SCRIPT = HERE / "auto_fix.py"
failures = 0


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("PASS  " if ok else "FAIL  ") + name + (("\n        " + detail) if detail else ""))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Sandbox:
    """A disposable copy of the demo vault plus its own backup store."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autofix-test-"))
        self.vault = self.tmp / "vault"
        shutil.copytree(DEMO, self.vault)
        self.rules = self.tmp / "rules.json"
        shutil.copy2(RULES_SRC, self.rules)
        self.backups = self.tmp / "backups"
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self, *args, env_extra=None):
        env = dict(os.environ)
        env.pop("KB_AUTOFIX_FORCE_VERIFY_FAIL", None)
        env.update(env_extra or {})
        p = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.vault), "--rules", str(self.rules),
             "--backup-dir", str(self.backups), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def backup_count(self):
        return len([d for d in self.backups.iterdir() if d.is_dir()]) if self.backups.is_dir() else 0

    def target(self):
        """First registered document with a readable number on a marked line."""
        rules = json.loads(self.rules.read_text(encoding="utf-8"))
        for consumer in rules["documents"]["consumers"]:
            path = self.vault / consumer["path"]
            if not path.exists():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "rules:" in line and re.search(r"(\d+)\s*(tags?|areas?|index)", line):
                    return path, i
        return None, None

    def break_line(self, path, lineno, replacement):
        with open(path, encoding="utf-8", newline="") as fh:   # keep CRLF as CRLF
            text = fh.read()
        lines = text.splitlines(keepends=True)
        lines[lineno - 1] = replacement(lines[lineno - 1])
        path.write_text("".join(lines), encoding="utf-8", newline="")
        return text, "".join(lines)


def off_by_one(line):
    m = re.search(r"(\d+)(\s*(?:tags?|areas?|index))", line)
    return line[:m.start(1)] + str(int(m.group(1)) - 1) + line[m.end(1):]


def in_words(line):
    m = re.search(r"(\d+)(\s*(?:tags?|areas?|index))", line)
    return line[:m.start(1)] + "seven" + line[m.end(1):]


def main():
    if not DEMO.is_dir() or not RULES_SRC.exists():
        report("environment", False, "demo-vault or rules.example.json missing")
        return 1

    # 1 + 2 + 7 --------------------------------------------------------------
    with Sandbox() as box:
        target, lineno = box.target()
        if target is None:
            report("environment", False, "no marked claim found in the demo vault")
            return 1
        original, broken = box.break_line(target, lineno, off_by_one)
        others = {p: sha(p) for p in box.vault.rglob("*.md") if p != target}

        before = sha(target)
        code, out = box.run()
        report("1 - dry run writes nothing",
               code == 0 and sha(target) == before and "->" in out,
               "exit=%d, file %s" % (code, "unchanged" if sha(target) == before else "MODIFIED"))

        code, out = box.run("--apply")
        report("2 - drift fixed back to the original bytes",
               code == 0 and target.read_text(encoding="utf-8") == original,
               "exit=%d | %s" % (code, out.strip().splitlines()[0] if out.strip() else ""))
        report("7 - unregistered files untouched",
               all(sha(p) == h for p, h in others.items()),
               "%d other note(s) checked" % len(others))
        report("2b - a backup was recorded", box.backup_count() == 1,
               "backups=%d" % box.backup_count())

        n = box.backup_count()
        code, out = box.run("--apply")
        report("3 - second run is a no-op",
               code == 0 and "nothing to fix" in out and box.backup_count() == n,
               "exit=%d, backups %d -> %d" % (code, n, box.backup_count()))

    # 4 ----------------------------------------------------------------------
    with Sandbox() as box:
        target, lineno = box.target()
        _, broken = box.break_line(target, lineno, in_words)
        code, out = box.run("--json")
        data = json.loads(out)
        report("4 - number in words is skipped, never guessed",
               code == 0 and not data["fixes"] and data["skipped"],
               "fixes=%d skipped=%d" % (len(data["fixes"]), len(data["skipped"])))
        code, _ = box.run("--apply")
        report("4b - apply leaves that line alone",
               code == 0 and target.read_text(encoding="utf-8") == broken, "exit=%d" % code)

    # 5 ----------------------------------------------------------------------
    with Sandbox() as box:
        target, lineno = box.target()
        _, broken = box.break_line(target, lineno, off_by_one)
        code, out = box.run("--apply", env_extra={"KB_AUTOFIX_FORCE_VERIFY_FAIL": "1"})
        rolled = target.read_text(encoding="utf-8") == broken
        report("5 - red gate after the fix rolls back and exits 1",
               code == 1 and rolled and "rolled back" in out.lower(),
               "exit=%d, file %s" % (code, "restored" if rolled else "NOT restored"))

    # 6 ----------------------------------------------------------------------
    with Sandbox() as box:
        target, lineno = box.target()
        original, broken = box.break_line(target, lineno, off_by_one)
        code, _ = box.run("--apply")
        fixed = target.read_text(encoding="utf-8")
        code2, _ = box.run("--rollback", "last", "--force")
        report("6 - rollback restores the pre-run state",
               code == 0 and code2 == 0 and fixed == original
               and target.read_text(encoding="utf-8") == broken,
               "apply=%d rollback=%d" % (code, code2))

    # 8 - CRLF files stay CRLF (the bug this suite exists to keep out) ---------
    with Sandbox() as box:
        target, lineno = box.target()
        crlf = target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        target.write_bytes(crlf)
        original, broken = box.break_line(target, lineno, off_by_one)
        before = target.read_bytes().count(b"\r\n")
        code, _ = box.run("--apply")
        after = target.read_bytes()
        lone_lf = after.count(b"\n") - after.count(b"\r\n")
        report("8 - a CRLF file is still CRLF after the fix",
               code == 0 and after.count(b"\r\n") == before and lone_lf == 0
               and after == crlf,
               "exit=%d, CRLF %d -> %d, bare LF %d"
               % (code, before, after.count(b"\r\n"), lone_lf))

    # 9 - two markers on ONE line that share a pattern -------------------------
    # The dangerous shape is two claims with DIFFERENT sources but the same number
    # pattern. This line used to be refused outright: the checker scanned the whole line
    # and handed both markers the leftmost number, so two fixes aimed at one column.
    # Each marker now owns the text before it, on both sides, so the line is fixable -
    # every number is rewritten in its own slot.
    def collision_box(second):
        """Sandbox whose rules define a second claim sharing the `tags` pattern."""
        box = Sandbox().__enter__()
        rules = json.loads(box.rules.read_text(encoding="utf-8"))
        rules["documents"]["claim_types"]["tag_count_alias"] = {
            "description": "same unit as tag_count, different source - the collision case",
            "source": "area_count", "patterns": [r"(\d+)\s*tags?\b"]}
        box.rules.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
        facts = json.loads(subprocess.run(
            [sys.executable, str(HERE / "check_rules_drift.py"), str(box.vault),
             "--rules", str(box.rules), "--show"],
            capture_output=True, text=True, encoding="utf-8").stdout)
        doc = box.vault / rules["documents"]["consumers"][0]["path"]
        with open(doc, "a", encoding="utf-8", newline="") as fh:
            fh.write("\nSummary: %d tags <!-- rules:tag_count --> of %d tags "
                     "<!-- rules:tag_count_alias -->\n"
                     % (facts["tag_count"] - 3, second(facts)))
        want = ("Summary: %d tags <!-- rules:tag_count --> of %d tags "
                "<!-- rules:tag_count_alias -->" % (facts["tag_count"], facts["area_count"]))
        return box, doc, want

    box, doc, want = collision_box(lambda f: f["tag_count"])
    try:
        planned = json.loads(box.run("--json")[1])
        code, out = box.run("--apply")
        line = next((l for l in doc.read_text(encoding="utf-8").splitlines()
                     if l.startswith("Summary:")), "line gone")
        report("9 - two markers sharing a pattern: each number fixed in its own slot",
               code == 0 and len(planned["fixes"]) == 2 and line == want,
               "exit=%d, fixes=%d | %s" % (code, len(planned["fixes"]), line))
    finally:
        box.__exit__()

    # 9b - the same line with both numbers ALREADY EQUAL. Only one fix is planned, so
    # nothing overlaps and drop_ambiguous never sees it; a fixer searching the whole line
    # would rewrite the leftmost match - the number that is already correct - and leave
    # the wrong one standing. This is the case owned_window exists for.
    box, doc, want = collision_box(lambda f: f["tag_count"] - 3)
    try:
        code, out = box.run("--apply")
        line = next((l for l in doc.read_text(encoding="utf-8").splitlines()
                     if l.startswith("Summary:")), "line gone")
        report("9b - equal numbers under two markers: the other marker's token is safe",
               code == 0 and line == want, "exit=%d | %s" % (code, line))
    finally:
        box.__exit__()

    # 9c - the overlap guard must still be alive. Disjoint segments mean production can
    # no longer produce an overlapping pair, so call it directly: a guard nobody can
    # trigger is a guard nobody would notice breaking.
    import importlib.util
    _spec = importlib.util.spec_from_file_location("auto_fix_mod", SCRIPT)
    _af = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_af)
    overlapping = [
        {"file": "x.md", "line": 1, "claim": "tag_count", "got": 5, "want": 7,
         "col": 10, "end": 11},
        {"file": "x.md", "line": 1, "claim": "tag_count_alias", "got": 5, "want": 5,
         "col": 10, "end": 11},
    ]
    kept, skipped = _af.drop_ambiguous(list(overlapping), [])
    report("9c - last-resort guard still refuses two fixes aimed at one token",
           kept == [] and any("same number" in s["why"] for s in skipped),
           "kept=%d, skipped=%d" % (len(kept), len(skipped)))

    # 10 - every neighbour is invoked in a shape that neighbour accepts ---------
    # The bug this exists for: a companion tool built on argparse SUBPARSERS wants its
    # global flags BEFORE the subcommand. Put them after and you get exit 2, which a
    # caller reading "non-zero" as "gate red" turns into a permanent, quiet refusal to
    # do any work - the failure mode that sounds like caution. Exit 0 or 1 are both fine
    # here (green gate / red gate); exit 2 means we are calling it wrong.
    with Sandbox() as box:
        shapes = [
            ("check_rules_drift.py", ["check_rules_drift.py", str(box.vault),
                                      "--rules", str(box.rules), "--json"]),
            ("verify_kb.py", ["verify_kb.py", str(box.vault), "--rules", str(box.rules)]),
            ("claim.py", ["claim.py", "check", "Ops/Index - Ops.md", "--vault", str(box.vault),
                          "--stream", "autofix-selftest"]),
        ]
        bad = []
        for label, argv in shapes:
            tool = HERE / argv[0]
            if not tool.exists():
                continue
            p = subprocess.run([sys.executable, str(tool), *argv[1:]], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if p.returncode == 2 or "unrecognized arguments" in (p.stderr or ""):
                bad.append("%s -> exit %d" % (label, p.returncode))
        report("10 - neighbours are called in a shape they accept (no usage errors)",
               not bad, "; ".join(bad) if bad else "%d invocation(s) accepted" % len(shapes))

    print()
    print("all cases passed" if not failures else "%d case(s) FAILED" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
