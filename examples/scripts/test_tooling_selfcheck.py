# -*- coding: utf-8 -*-
"""
test_tooling_selfcheck.py - break-the-gate tests for tooling_selfcheck.py.

A gate that is always green is worse than no gate: it manufactures confidence. These
cases build a throwaway vault in the system temp directory with toy suites of every kind
- passing, failing, hanging - and demand the runner react correctly: discover, catch red,
respect the green marker, and block (or refrain from blocking) at exactly the right
moments. Every command is given an explicit --vault and --state under temp, so a real
vault is never touched.

Run:   python examples/scripts/test_tooling_selfcheck.py
Exit:  0 = all cases pass, 1 = at least one failed
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT = Path(__file__).resolve().parent / "tooling_selfcheck.py"
failures = 0
vault = Path()
state = Path()

TEST_OK = "import pathlib\n" \
          "p = pathlib.Path(__file__).with_name('runs.txt')\n" \
          "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n" \
          "print('toy suite ok')\n"
TEST_RED = "print('toy suite red')\nraise SystemExit(1)\n"
TEST_SLOW = "import time\ntime.sleep(9)\n"


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok and detail:
        print("---- output ----\n" + detail.strip() + "\n----------------")


def run(argv, stdin=None, env_extra=None):
    env = dict(os.environ)
    for k in ("KB_TOOLING_GATE_OFF", "KB_TOOLING_STATE", "KB_TOOLING_TIMEOUT"):
        env.pop(k, None)
    env["KB_TOOLING_ROOTS"] = "Ops"
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-B", str(SCRIPT)] + argv,
                       input=(stdin or "").encode("utf-8") if stdin is not None else None,
                       capture_output=True, cwd=str(vault), env=env, timeout=120)
    dec = lambda b: (b or b"").decode("utf-8", "replace")   # noqa: E731
    return p.returncode, dec(p.stdout) + dec(p.stderr)


def att(folder="Gate Demo") -> Path:
    p = vault / "Ops" / folder / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_test(name, body, folder="Gate Demo"):
    att(folder).joinpath(name).write_text(body, encoding="utf-8")


def runs_count(folder="Gate Demo") -> int:
    p = att(folder) / "runs.txt"
    return int(p.read_text()) if p.is_file() else 0


def stop_payload(active=False) -> str:
    return json.dumps({"session_id": "s1", "hook_event_name": "Stop", "cwd": str(vault),
                       "stop_hook_active": active})


def base(*extra):
    return ["--vault", str(vault), "--state", str(state)] + list(extra)


def main() -> int:
    global vault, state
    tmp = Path(tempfile.mkdtemp(prefix="toolgate-test-"))
    vault = tmp / "demo vault"                    # a space in the path, on purpose
    (vault / ".obsidian").mkdir(parents=True)
    state = tmp / "cache" / "green-marker.json"
    try:
        # --- discovery -------------------------------------------------------------
        write_test("test_ok.py", TEST_OK)
        att().joinpath("tool_x.py").write_text("# a tool with no test\n", encoding="utf-8")
        (vault / ".hidden" / "tests").mkdir(parents=True)
        (vault / ".hidden" / "tests" / "test_ignored.py").write_text("x=1\n", encoding="utf-8")
        (vault / "Ops" / "Gate Demo" / "test_outside_attachments.py").write_text(
            "x=1\n", encoding="utf-8")

        code, out = run(base("list"))
        report("1. discovers tests under attachments/", code == 0 and "test_ok.py" in out, out)
        report("2. ignores dot-directories (they own their own checks)",
               "test_ignored" not in out, out)
        report("3. ignores .py outside an attachments/ folder",
               "test_outside_attachments" not in out, out)
        report("4. reports tools with no test beside them", "tool_x.py" in out, out)

        # --- running ---------------------------------------------------------------
        code, out = run(base("run"))
        report("5. all suites green -> exit 0", code == 0 and "ALL PASS" in out, out)
        report("6. a green run writes the marker", state.is_file(), str(state))
        report("7. suites really execute (not just counted on disk)", runs_count() == 1,
               str(runs_count()))

        code, out = run(base("run", "--if-stale"))
        report("8. --if-stale skips when nothing changed",
               code == 0 and "Skipped" in out and runs_count() == 1, out)

        att().joinpath("tool_x.py").write_text("# edited\n", encoding="utf-8")
        time.sleep(1.1)                           # second-resolution mtime
        att().joinpath("tool_x.py").write_text("# edited twice\n", encoding="utf-8")
        code, out = run(base("run", "--if-stale"))
        report("9. touching a TOOL re-runs the suite, not just touching a test",
               code == 0 and "Running because" in out and runs_count() == 2, out)

        # --- catching red ----------------------------------------------------------
        write_test("test_red.py", TEST_RED)
        code, out = run(base("run"))
        report("10. a red suite -> exit 1, named in the summary",
               code == 1 and "FAIL test_red.py" in out and "FAILED 1/2" in out, out)
        report("11. the tail of the failing suite's output is shown", "toy suite red" in out, out)

        before = json.loads(state.read_text(encoding="utf-8")).get("last_ok")
        code, out = run(base("run"))
        after = json.loads(state.read_text(encoding="utf-8")).get("last_ok")
        report("12. a RED run never updates the green marker (the gate must keep firing)",
               code == 1 and before == after, "%s vs %s" % (before, after))

        # --- the Stop hook ---------------------------------------------------------
        code, out = run(base("hook-stop"), stdin=stop_payload())
        report("13. hook-stop: changed tooling + red suite -> exit 2 blocks the turn",
               code == 2 and "TOOLING GATE" in out, out)

        code, out = run(base("hook-stop"), stdin=stop_payload(active=True))
        report("14. already blocked this turn -> does not block again (no trap)",
               code == 0 and "Already blocked" in out, out)

        code, out = run(base("hook-stop"), stdin=stop_payload(),
                        env_extra={"KB_TOOLING_GATE_OFF": "1"})
        report("15. KB_TOOLING_GATE_OFF=1 disables the gate", code == 0 and out.strip() == "", out)

        code, out = run(base("hook-stop"), stdin="{ not json")
        report("16. a malformed payload does not crash the hook", code in (0, 2), out)

        att().joinpath("test_red.py").unlink()
        run(base("run"))                          # back to green, new marker
        n = runs_count()
        code, out = run(base("hook-stop"), stdin=stop_payload())
        report("17. hook-stop with nothing changed is silent and runs no suite",
               code == 0 and out.strip() == "" and runs_count() == n, out + str(runs_count()))

        # --- edges -----------------------------------------------------------------
        write_test("test_slow.py", TEST_SLOW)
        code, out = run(base("run", "--timeout", "2"))
        report("18. a hanging suite is failed on timeout, never hangs the gate",
               code == 1 and "FAIL test_slow.py" in out and "exceeded 2s" in out, out)
        att().joinpath("test_slow.py").unlink()

        shutil.rmtree(att(), ignore_errors=True)
        code, out = run(base("run"))
        report("19. a vault with no suites exits 0 and says so",
               code == 0 and "No suites found" in out, out)

        code, out = run(["run", "--vault", str(tmp / "does-not-exist"), "--state", str(state)])
        report("20. a --vault typo is rejected instead of reporting a false green",
               code == 2 and "not a vault root" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRESULT: %s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
