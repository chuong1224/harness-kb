# -*- coding: utf-8 -*-
"""
test_tooling_selfcheck.py - break-the-gate tests for tooling_selfcheck.py.

A gate that is always green is worse than no gate: it manufactures confidence. These
cases build a throwaway vault in the system temp directory with toy suites of every kind
- passing, failing, hanging - and demand the runner react correctly: discover, catch red,
respect the green marker, and block (or refrain from blocking) at exactly the right
moments. Every command is given an explicit --vault under temp; all but the namespace
regression also use an explicit --state. The default-state cases redirect LOCALAPPDATA
under the same temp root, so a real vault and its marker are never touched.

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
TMP_PREFIX = "toolgate-test-"
failures = 0
vault = None
state = None
temp_root = None

TEST_OK = "import pathlib\n" \
          "p = pathlib.Path(__file__).with_name('runs.txt')\n" \
          "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n" \
          "print('toy suite ok')\n" \
          "print('[PASS] toy assertion one')\n" \
          "print('[PASS] toy assertion two')\n"
TEST_RED = "print('toy suite red')\nraise SystemExit(1)\n"
TEST_SLOW = "import time\ntime.sleep(9)\n"

# One assertion instead of two: the shape of a silent skip, or of deleted cases.
TEST_THIN = "print('[PASS] toy assertion one')\n"
# Green, exits 0, asserts nothing, declares nothing - the suite the coverage count cannot
# see, because 0 cannot fall.
TEST_MUTE = "print('did some work')\n"
# Declares its gaps in full: not measured, but not silent either.
TEST_SKIPPED = "print('[SKIP] needs a browser that is not installed here')\n"
# A bare-assert suite reporting one roll-up line. Counting lines would peg it at 1.
# Deliberately ASCII: a toy suite printing a non-ASCII separator would die on a console
# whose redirected encoding is not UTF-8, and that failure would look like a gate bug.
TEST_ROLLUP = "print('PASS 7/7')\n"
# The interpreter genuinely lacks this module.
TEST_NOLIB = "import definitely_not_installed_xyz\nprint('[PASS] never reached')\n"
# The dangerous half: catches the ImportError, declares a skip, exits 0.
TEST_NOLIB_SKIP = "try:\n" \
                  "    import definitely_not_installed_xyz\n" \
                  "except ImportError:\n" \
                  "    print(\"[SKIP] No module named 'definitely_not_installed_xyz'\")\n"
# Red while PRINTING a lock error about a document that is not locked at all: the label
# must not be handed out on the strength of the text.
TEST_FAKE_LOCK = "print('[PASS] one')\n" \
                 'print("PermissionError: [Errno 13] Permission denied: \'ledger.xlsx\'")\n' \
                 "raise SystemExit(1)\n"
# A real permission error, on a file the diagnosis does not cover.
TEST_LOCK_OTHER = "print('[PASS] one')\n" \
                  'print("PermissionError: [Errno 13] Permission denied: \'config.json\'")\n' \
                  "raise SystemExit(1)\n"
# GREEN, and mentions the string in its own toy data. Must stay a pass.
TEST_GREEN_MENTIONS = "print('[PASS] handles the message')\n" \
                      'print("sample: PermissionError: [Errno 13] denied: \'book.xlsx\'")\n'


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok and detail:
        print("---- output ----\n" + detail.strip() + "\n----------------")


def own_temp(p):
    """Accept only paths owned by this exact test run; every deletion goes through here."""
    if p is None or temp_root is None:
        return False
    try:
        candidate = Path(p).resolve()
        owner = temp_root.resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    owner_is_safe = (
        owner != system_temp
        and system_temp in owner.parents
        and owner.name.startswith(TMP_PREFIX)
    )
    return owner_is_safe and (candidate == owner or owner in candidate.parents)


def wipe(p):
    """Delete only directories owned by this test run; reject everything else."""
    if own_temp(p):
        shutil.rmtree(p, ignore_errors=True)


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


def reset():
    """Start a block from zero: no suites on disk, no green marker."""
    wipe(att())
    att().mkdir(parents=True, exist_ok=True)
    try:
        state.unlink()
    except OSError:
        pass


def mark() -> dict:
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hold_file(path: Path):
    """Hold `path` so that opening it for READING is refused, the way a desktop office
    application holds its document. Returns a release callable, or None when this platform
    (or this user) cannot produce that state - in which case the caller declares a [SKIP]
    rather than pretending the case ran."""
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, share=0
        if handle in (0, -1, 2 ** 64 - 1):
            return None
        return lambda: ctypes.windll.kernel32.CloseHandle(handle)
    # POSIX has no mandatory locking; mode 0 is the portable way to make open() refuse -
    # except for root, who ignores it, so the effect is verified before being relied on.
    try:
        original = path.stat().st_mode
        os.chmod(str(path), 0)
        with open(str(path), "rb"):
            pass
    except PermissionError:
        return lambda: os.chmod(str(path), original)
    except OSError:
        return None
    os.chmod(str(path), original)                 # it opened anyway: no lock to be had
    return None


def main() -> int:
    global vault, state, temp_root
    tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    temp_root = tmp.resolve()
    vault = tmp / "demo vault"                    # a space in the path, on purpose
    (vault / ".obsidian").mkdir(parents=True)
    state = tmp / "cache" / "green-marker.json"
    try:
        # Destructive-delete fuse: inspect paths only; never delete the unsafe examples.
        report("S1. own_temp rejects the current directory",
               not own_temp(Path()) and not own_temp(Path.cwd()))
        report("S2. own_temp rejects the system temp root", not own_temp(tempfile.gettempdir()))
        report("S3. own_temp accepts this run's root and descendants",
               own_temp(tmp) and own_temp(vault) and own_temp(state.parent))
        report("S4. own_temp rejects a same-prefix directory from another run",
               not own_temp(Path(tempfile.gettempdir()) / "toolgate-test-not-this-run"))

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
               code == 1 and "FAILED 1/2" in out and "test_red.py" in out, out)
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
               code == 1 and "test_slow.py" in out and "exceeded 2s" in out, out)
        att().joinpath("test_slow.py").unlink()

        wipe(att())
        run(base("forget"))     # no prior mark: an empty vault is genuinely nothing to do
        code, out = run(base("run"))
        report("19. a vault with no suites, and no mark to lose, exits 0 and says so",
               code == 0 and "No suites found" in out, out)

        code, out = run(["run", "--vault", str(tmp / "does-not-exist"), "--state", str(state)])
        report("20. a --vault typo is rejected instead of reporting a false green",
               code == 2 and "not a vault root" in out, out)

        # --- measure the suite, do not ask it ---------------------------------------
        reset()
        write_test("test_ok.py", TEST_OK)                  # two assertion lines
        write_test("test_rollup.py", TEST_ROLLUP)          # seven, via one roll-up line
        code, out = run(base("run"))
        report("21. assertions are counted from the output, and shown per suite",
               code == 0 and "2 assertions" in out, out)
        report("22. a roll-up `PASS n/m` counts the LEFT side, not the single line",
               code == 0 and "7 assertions" in out, out)
        report("23. the green mark stores the per-suite counts to compare against",
               mark().get("counts", {}).get("test_ok.py") == 2, json.dumps(mark()))

        write_test("test_ok.py", TEST_THIN)                # a case quietly disappears
        code, out = run(base("run"))
        report("24. green suites measuring LESS than last time -> blocked",
               code == 1 and "COVERAGE FELL" in out, out)
        report("25. the drop names the suite and both numbers",
               "test_ok.py: 2 -> 1 assertions" in out, out)
        report("26. no tolerance threshold: losing one assertion is enough",
               code == 1, out)

        code, out = run(base("accept", "--reason", "the second case was redundant"))
        report("27. accept --reason lowers the mark instead of switching the gate off",
               code == 0 and "ACCEPTED" in out, out)
        report("28. the acceptance is recorded with its reason, not applied silently",
               (mark().get("accepted") or [{}])[-1].get("reason") == "the second case was"
               " redundant", json.dumps(mark().get("accepted")))
        code, out = run(base("run"))
        report("29. after accepting, the lowered count is the new baseline",
               code == 0 and "ALL PASS" in out, out)

        code, out = run(base("accept"))
        report("30. accept with no reason is refused, never defaulted",
               code == 2 and "requires --reason" in out, out)

        att().joinpath("test_rollup.py").unlink()
        code, out = run(base("run"))
        report("31. deleting a whole suite is a coverage loss too (no output to inspect)",
               code == 1 and "gone from disk" in out and "test_rollup.py" in out, out)

        # --- the value that has no delta --------------------------------------------
        reset()
        write_test("test_ok.py", TEST_OK)
        write_test("test_mute.py", TEST_MUTE)
        code, out = run(base("run"))
        report("32. a suite that runs green while asserting nothing is refused",
               code == 1 and "MUTE SUITE" in out and "test_mute.py" in out, out)
        report("33. a mute run records no green mark (or the next turn goes quiet)",
               not state.is_file(), str(state))
        code, out = run(base("accept", "--reason", "let it be"))
        report("34. mute gets NO acceptance path - silence is not a lowered mark",
               code == 1 and "MUTE SUITE" in out, out)

        att().joinpath("test_mute.py").unlink()
        write_test("test_skipped.py", TEST_SKIPPED)
        code, out = run(base("run"))
        report("35. a suite that declares its gaps in full is not mute",
               code == 0 and "MUTE" not in out and "NOT FULLY MEASURED" in out, out)

        att().joinpath("test_skipped.py").unlink()
        write_test("test_silent_red.py", "raise SystemExit(1)\n")
        code, out = run(base("run"))
        report("36. a RED suite that printed nothing is reported red, not mute",
               code == 1 and "MUTE" not in out, out)

        # --- broken measurement: a missing library ----------------------------------
        reset()
        write_test("test_ok.py", TEST_OK)
        write_test("test_nolib.py", TEST_NOLIB)
        code, out = run(base("run"))
        report("37. a missing library is MISSING-LIB, not FAIL",
               code == 1 and "MISSING-LIB" in out and "could not be measured" in out, out)
        report("38. it blocks exactly as hard as red - a label is not an escape hatch",
               code == 1 and not state.is_file(), out)
        report("39. the repair patches the interpreter that ran the tests, not the code",
               "-m pip install" in out and "Do NOT edit the code" in out, out)

        att().joinpath("test_nolib.py").unlink()
        write_test("test_nolib_skip.py", TEST_NOLIB_SKIP)
        code, out = run(base("run"))
        report("40. false green: exits 0 but declares a missing lib -> still caught",
               code == 1 and "MISSING-LIB" in out, out)

        # --- broken measurement: a document held open -------------------------------
        reset()
        write_test("test_ok.py", TEST_OK)
        doc = att() / "ledger.xlsx"
        doc.write_bytes(b"toy workbook")
        write_test("test_reads_doc.py",
                   "import pathlib\n"
                   "print('[PASS] opens the workbook')\n"
                   "open(pathlib.Path(__file__).with_name('ledger.xlsx'), 'rb').read()\n")
        release = hold_file(doc)
        if release:
            code, out = run(base("run"))
            report("41. a document genuinely held open is LOCKED-FILE, not FAIL",
                   code == 1 and "LOCKED-FILE" in out, out)
            report("42. it blocks and records no green mark",
                   code == 1 and not state.is_file(), out)
            report("43. the message says it re-checked, and names the document",
                   "RE-CHECKED" in out and "ledger.xlsx" in out, out)
            release()
            code, out = run(base("run"))
            report("44. closing the document makes it green - the code was never broken",
                   code == 0 and "ALL PASS" in out, out)
        else:
            for n, what in ((41, "a document genuinely held open is LOCKED-FILE"),
                            (42, "it blocks and records no green mark"),
                            (43, "the message says it re-checked, and names the document"),
                            (44, "closing the document makes it green")):
                print("[SKIP] %d. %s - this platform cannot hold a file that way" % (n, what))

        # The re-derivation cases need no real lock: they are about NOT trusting the text.
        reset()
        write_test("test_ok.py", TEST_OK)
        write_test("test_fake_lock.py", TEST_FAKE_LOCK)
        code, out = run(base("run"))
        report("45. output CLAIMING a lock, on a file that opens fine, stays FAIL",
               code == 1 and "LOCKED-FILE" not in out and "FAILED" in out, out)

        att().joinpath("test_fake_lock.py").unlink()
        write_test("test_lock_other.py", TEST_LOCK_OTHER)
        code, out = run(base("run"))
        report("46. a permission error on a non-office file is never given that diagnosis",
               code == 1 and "LOCKED-FILE" not in out, out)

        att().joinpath("test_lock_other.py").unlink()
        write_test("test_mentions.py", TEST_GREEN_MENTIONS)
        code, out = run(base("run"))
        report("47. a GREEN suite printing that string keeps its pass",
               code == 0 and "ALL PASS" in out, out)

        # --- the hook names the cause -----------------------------------------------
        reset()
        write_test("test_ok.py", TEST_OK)
        run(base("run"))                                   # establish a green mark
        write_test("test_mute.py", TEST_MUTE)
        code, out = run(base("hook-stop"), stdin=stop_payload())
        report("48. the hook names the CAUSE, instead of blaming code that is fine",
               code == 2 and "asserting NOTHING" in out, out)

        # --- the instrument itself has to be deterministic ---------------------------
        reset()
        # A separator outside ASCII, in the one place where losing it changes a NUMBER:
        # if the child's output is decoded through a locale codec, this arrives mangled,
        # the roll-up pattern misses, and the suite counts 1 instead of 7 - a coverage
        # mark that drifts with whichever shell launched the runner.
        write_test("test_unicode.py", "print('PASS 7/7 \\u00b7 toy rollup suite')\n")
        code, out = run(base("run"))
        report("49. non-ASCII from a suite survives the pipe, so the count cannot drift",
               code == 0 and "7 assertions" in out, out)

        # --- one host, two vaults -------------------------------------------------
        # The default state used to be keyed by hostname alone. Running this vault first
        # and another vault second therefore made the second run inherit test_unicode.py
        # as vanished coverage. Worse, an accepted or green run could overwrite the first
        # vault's mark. Both commands deliberately omit --state to exercise the default.
        default_cache = tmp / "default-cache"
        default_env = {"LOCALAPPDATA": str(default_cache)}
        code, out = run(["--vault", str(vault), "run"], env_extra=default_env)
        marks_before = set((default_cache / "kb-tooling-selfcheck").glob("state-*.json"))
        report("50. the default marker is written in a vault-specific namespace",
               code == 0 and len(marks_before) == 1, out)

        other_vault = tmp / "other vault"
        (other_vault / ".obsidian").mkdir(parents=True)
        other_test = other_vault / "Ops" / "Other Gate" / "attachments" / "test_other.py"
        other_test.parent.mkdir(parents=True)
        other_test.write_text("print('[PASS] other vault assertion')\n", encoding="utf-8")
        code, out = run(["--vault", str(other_vault), "run"], env_extra=default_env)
        marks_after = set((default_cache / "kb-tooling-selfcheck").glob("state-*.json"))
        report("51. a second vault on the same host does not inherit the first vault's tests",
               code == 0 and "test_other.py" in out and "test_unicode.py" not in out, out)
        report("52. the two vaults keep separate default markers",
               len(marks_after) == 2 and marks_before < marks_after,
               "before=%r after=%r" % (marks_before, marks_after))
    finally:
        wipe(tmp)
        vault = None
        state = None
        temp_root = None

    print("\nRESULT: %s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
