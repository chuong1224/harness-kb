#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_audit_gate.py - break-the-gate tests for audit_gate.py.

Everything runs against a throwaway vault under the system temp directory with stub
gates, so the suite never touches a real knowledge base. That is not excess caution: a
guard whose own tests dirty the thing it guards has already failed at its job.

The cases aim at the ways this guard could be *harmful* rather than the happy path:

  * it blocks a finding that is genuinely new, and does NOT block inherited debt - the
    "blocks too much" failure is the one that makes people switch a guard off;
  * blocking does not write the baseline, or the very next turn would grandfather the
    finding it just blocked on;
  * `stop_hook_active` ends the loop, because a jailed session is worse than a missed
    finding;
  * a gate that fails with unreadable output still counts as failing - a checker whose
    format drifts must never be silently downgraded to "clean";
  * a bad --vault reports an error instead of reporting "clean" and writing a false
    baseline;
  * `paths` keeps an expensive gate off the critical path until its files change.
  * two vaults using the same machine cache never share a baseline or an accepted finding.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass

HERE = Path(__file__).resolve().parent
GATE = HERE / "audit_gate.py"

fails: list = []


def check(name: str, cond: bool, info: str = "") -> None:
    if not cond:
        fails.append(name)
    print("  %s %-58s %s" % ("PASS" if cond else "FAIL", name, info))


STUB = '''import os, sys
name = %r
items = [x for x in (os.environ.get("STUB_" + name.upper().replace("-", "_")) or "").split("|") if x]
log = os.environ.get("STUB_LOG")
if log:
    open(log, "a", encoding="utf-8").write(name + "\\n")
%s
sys.exit(1 if items else 0)
'''

BODY = {
    "integrity": 'print(__import__("json").dumps({"ok": not items, "errors": items}))',
    "suite": 'for x in items: print("FAIL " + x + " (2.5s)")',
}

CONFIG = {
    "gates": [
        {"name": "integrity", "parse": "json:errors", "paths": ["**"],
         "cmd": ["{python}", "tools/integrity.py"]},
        {"name": "suite", "parse": "lines:^FAIL\\s+(.*)$", "paths": ["code/**"],
         "cmd": ["{python}", "tools/suite.py"]},
        {"name": "absent", "parse": "exit", "paths": ["**"],
         "cmd": ["{python}", "tools/not-installed-here.py"]},
    ]
}


def build_vault(root: Path) -> None:
    (root / ".obsidian").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "code").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "code" / "app.py").write_text("# app\n", encoding="utf-8")
    (root / "tools" / "integrity.py").write_text(STUB % ("integrity", BODY["integrity"]),
                                                 encoding="utf-8")
    (root / "tools" / "suite.py").write_text(STUB % ("suite", BODY["suite"]), encoding="utf-8")
    (root / "gates.json").write_text(json.dumps(CONFIG, indent=1), encoding="utf-8")


def run(vault: Path, state: Path | None, *args, payload=None, env=None, raw=None):
    e = dict(os.environ)
    e.pop("AUDIT_GATE_OFF", None)
    for k in list(e):
        if k.startswith("STUB_"):
            e.pop(k)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    cwd = vault if vault.is_dir() else HERE
    argv = [sys.executable, "-B", str(GATE), *args, "--vault", str(vault)]
    if state is not None:
        argv += ["--state", str(state)]
    p = subprocess.run(argv,
                       input=data, capture_output=True, cwd=str(cwd), env=e)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out


def touch(p: Path, text: str) -> None:
    """Push mtime forward. The fingerprint stores whole seconds, so two writes of equal
    length inside one second look identical and the test would pass for the wrong reason."""
    p.write_text(text, encoding="utf-8")
    t = time.time() + 2
    os.utime(p, (t, t))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="audit-gate-test-"))
    vault, state, log = tmp / "vault", tmp / "state.json", tmp / "ran.log"
    try:
        build_vault(vault)
        print("Throwaway vault: %s" % vault)

        print("\n1. clean run, then the quiet path")
        code, out = run(vault, state, "run")
        check("first run is clean", code == 0, "exit=%d" % code)
        code, out = run(vault, state, "run")
        check("second run skips - nothing changed", code == 0 and "Skipped" in out)

        print("\n2. a new finding blocks")
        touch(vault / "notes" / "a.md", "# a2\n")
        bad = {"STUB_INTEGRITY": "notes/a.md: dangling link"}
        code, out = run(vault, state, "run", env=bad)
        check("run exits 1 on a new finding", code == 1, "exit=%d" % code)
        check("the finding is named", "dangling link" in out)
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)}, env=bad)
        check("hook-stop blocks with exit 2", code == 2, "exit=%d" % code)
        check("the message offers accept", "accept --why" in out)

        print("\n3. blocking must not write the baseline")
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)}, env=bad)
        check("still blocks on the next turn", code == 2, "exit=%d" % code)

        print("\n4. stop_hook_active ends the loop")
        code, out = run(vault, state, "hook-stop", env=bad,
                        payload={"cwd": str(vault), "stop_hook_active": True})
        check("does not block twice in one turn", code == 0, "exit=%d" % code)

        print("\n5. accept demands a reason, then tolerates the debt")
        code, out = run(vault, state, "accept", env=bad)
        check("accept without --why is refused", code == 2, "exit=%d" % code)
        code, out = run(vault, state, "accept", "--why", "left by another session", env=bad)
        check("accept with --why succeeds", code == 0, "exit=%d" % code)
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)}, env=bad)
        check("inherited debt does not block", code == 0, "exit=%d" % code)

        print("\n6. inherited debt tolerated, a new finding on top still blocks")
        touch(vault / "notes" / "a.md", "# a3\n")
        worse = {"STUB_INTEGRITY": bad["STUB_INTEGRITY"] + "|notes/b.md: brand new problem"}
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)}, env=worse)
        check("blocks on the new one", code == 2, "exit=%d" % code)
        check("only the new one is called new", "brand new problem" in out)
        check("inherited debt is named as not-the-blocker", "inherited finding" in out)

        print("\n7. paths keeps a gate off the critical path")
        run(vault, state, "forget")
        run(vault, state, "run")
        touch(vault / "notes" / "a.md", "# a4\n")
        log.write_text("", encoding="utf-8")
        run(vault, state, "run", env={"STUB_LOG": str(log)})
        ran = log.read_text(encoding="utf-8").split()
        check("a note change does not run the code suite", "suite" not in ran, "ran: %s" % ran)
        touch(vault / "code" / "app.py", "# app2\n")
        log.write_text("", encoding="utf-8")
        run(vault, state, "run", env={"STUB_LOG": str(log)})
        check("a code change does run it", "suite" in log.read_text(encoding="utf-8").split())

        print("\n8. a failing gate with unreadable output is still a finding")
        run(vault, state, "forget")
        run(vault, state, "run")
        touch(vault / "notes" / "a.md", "# a5\n")
        keep = (vault / "tools" / "integrity.py").read_text(encoding="utf-8")
        touch(vault / "tools" / "integrity.py",
              "import sys\nprint('not json at all')\nsys.exit(1)\n")
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)})
        check("unreadable failure still blocks", code == 2, "exit=%d" % code)
        check("it says the output was unreadable", "not readable JSON" in out)
        touch(vault / "tools" / "integrity.py", keep)

        print("\n9. a gate whose command is absent is skipped, not failed")
        run(vault, state, "forget")
        code, out = run(vault, state, "run")
        check("missing command does not fail the run", code == 0, "exit=%d" % code)
        check("it is reported as skipped", "absent" in out)

        print("\n10. a bad --vault must not read as clean")
        code, out = run(tmp / "nowhere", state, "run")
        check("bad vault exits 2 and explains", code == 2 and "no vault found" in out,
              "exit=%d" % code)

        print("\n11. kill switch and failing open")
        code, out = run(vault, state, "hook-stop", payload={"cwd": str(vault)},
                        env=dict(bad, AUDIT_GATE_OFF="1"))
        check("AUDIT_GATE_OFF=1 lets the turn end", code == 0, "exit=%d" % code)
        code, out = run(vault, state, "hook-stop", raw=b"this is not json")
        check("a broken payload does not jail the session", code in (0, 2), "exit=%d" % code)

        print("\n12. status reports the baseline it is holding")
        run(vault, state, "forget")
        run(vault, state, "run")
        touch(vault / "notes" / "a.md", "# a6\n")
        run(vault, state, "accept", "--why", "checking status output", env=bad)
        code, out = run(vault, state, "status")
        check("status lists the inherited finding", code == 0 and "dangling link" in out)
        check("status shows why it was accepted", "checking status output" in out)

        print("\n13. the shipped default config actually runs this repo's own checkers")
        # Without a gates.json the built-in default is used, and its relative paths must
        # resolve from the repo root regardless of where the caller stands. Shipping a
        # default nobody executed is how a reference implementation greets its first user
        # with an argparse error - `verify_kb.py` has no --json, and an earlier draft of
        # this default passed it one.
        demo = HERE.parent / "demo-vault"
        code, out = run(demo, tmp / "demo-state.json", "run", "--force")
        check("default config runs clean on examples/demo-vault", code == 0, "exit=%d" % code)
        check("both default gates actually ran",
              "integrity" in out and "rules-drift" in out)

        print("\n14. default state is namespaced by vault, not only by hostname")
        vault_a, vault_b = tmp / "namespace-a", tmp / "namespace-b"
        build_vault(vault_a)
        build_vault(vault_b)
        cache = tmp / "default-cache"
        cache_env = {"LOCALAPPDATA": str(cache)}
        code_a, out_a = run(vault_a, None, "run", env=cache_env)
        code_b, out_b = run(vault_b, None, "run", env=cache_env)
        states = sorted((cache / "audit-gate").glob("state-*.json"))
        check("two vaults create two default baseline files",
              code_a == 0 and code_b == 0 and len(states) == 2,
              "states=%d" % len(states))

        touch(vault_a / "notes" / "a.md", "# namespace a changed\n")
        shared_finding = dict(cache_env, STUB_INTEGRITY="same finding in either vault")
        code, out = run(vault_a, None, "accept", "--why", "owned by vault A",
                        env=shared_finding)
        check("vault A can accept its own finding", code == 0, "exit=%d" % code)
        touch(vault_b / "notes" / "a.md", "# namespace b changed\n")
        code, out = run(vault_b, None, "hook-stop", payload={"cwd": str(vault_b)},
                        env=shared_finding)
        check("vault A acceptance cannot silence vault B",
              code == 2 and "same finding" in out, "exit=%d" % code)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("RESULT: %s" % ("ALL PASS" if not fails else "FAIL %d - %s"
                          % (len(fails), ", ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
