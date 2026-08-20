# -*- coding: utf-8 -*-
"""test_derived_write_guard.py — contract-breaking tests for derived_write_guard.py.

This guard exists for a failure that produces no error at all: a generator running on a
machine whose state is empty, cheerfully rewriting a shared artifact with a fraction of its
rows and exiting 0. So the tests are less about the happy path and more about the two ways
a guard like this betrays you:

  * it lets the shrink through — the loss it was written to prevent;
  * it blocks something harmless — a first run, a restyled template, a normal growing run —
    at which point somebody switches it off and you are back to the first bullet.

Each case builds its own artifact and ledger under %TEMP% with a private prefix and removes
only what it created.

Run:   python test_derived_write_guard.py
Exit:  0 = all pass · 1 = something failed
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "derived_write_guard.py"
TMP_PREFIX = "harness-dwg-"
failures = 0

_spec = importlib.util.spec_from_file_location("derived_write_guard_mod", SCRIPT)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok and detail:
        print("---- output ----\n" + str(detail).strip() + "\n----------------")


def own_temp(p):
    """Only directories this suite created in %TEMP%."""
    if not p:
        return False
    p = Path(p).resolve()
    return p.name.startswith(TMP_PREFIX) and Path(tempfile.gettempdir()).resolve() in p.parents


def cleanup(*paths):
    for p in paths:
        if own_temp(p):
            shutil.rmtree(p, ignore_errors=True)


def stage(n_ledger=0):
    """(root, ledger_path, artifact_path) with a ledger of n synthetic records."""
    root = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    ledger = root / "ledger.json"
    ledger.write_text(
        json.dumps({"rec-%03d" % i: "value-%d" % i for i in range(n_ledger)}),
        encoding="utf-8",
    )
    return root, ledger, root / "log.md"


def write_artifact(path, claimed):
    path.write_text(
        "# Derived log\n\n**%s** %d\n\n| Record | Value |\n|---|---|\n| `x` | y |\n"
        % (g.COUNT_LABEL, claimed),
        encoding="utf-8",
        newline="\n",
    )


# --------------------------------------------------------------------- the loss it prevents

def case_refuses_the_shrink():
    root, ledger, out = stage(n_ledger=3)
    try:
        write_artifact(out, 166)
        before = out.read_text(encoding="utf-8")
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "refuses to write 3 records over an artifact claiming 166, exit 2, file untouched",
            code == 2 and out.read_text(encoding="utf-8") == before,
            "code=%s changed=%s" % (code, out.read_text(encoding="utf-8") != before),
        )
    finally:
        cleanup(root)


def case_message_names_the_loss_and_the_state():
    root, ledger, out = stage(n_ledger=3)
    try:
        write_artifact(out, 166)
        ok, msg = g.shrink_guard(3, str(out), state_path=str(ledger))
        report(
            "refusal names the row count lost and the state file it read, not just 'error'",
            ok is False and "163" in msg and "ledger.json" in msg and "--allow-shrink" in msg,
            msg,
        )
    finally:
        cleanup(root)


def case_empty_state_is_the_dangerous_case():
    """The real incident: a second machine with no ledger at all."""
    root, ledger, out = stage(n_ledger=0)
    try:
        write_artifact(out, 166)
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "a machine with an EMPTY ledger is refused, not treated as 'nothing to lose'",
            code == 2 and "166" in out.read_text(encoding="utf-8"),
            "code=%s" % code,
        )
    finally:
        cleanup(root)


# ------------------------------------------------------------------ the ways it blocks wrongly

def case_first_run_is_not_a_shrink():
    root, ledger, out = stage(n_ledger=2)
    try:
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "first run against a missing artifact writes normally (fails OPEN, exit 0)",
            code == 0 and out.exists() and "%s** 2" % g.COUNT_LABEL in out.read_text(encoding="utf-8"),
            "code=%s exists=%s" % (code, out.exists()),
        )
    finally:
        cleanup(root)


def case_unreadable_count_fails_open():
    root, ledger, out = stage(n_ledger=2)
    try:
        out.write_text("# Derived log\n\nsomebody restyled this template\n", encoding="utf-8")
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "artifact with no count line fails OPEN — a restyle must not become an outage",
            code == 0 and "%s** 2" % g.COUNT_LABEL in out.read_text(encoding="utf-8"),
            "code=%s" % code,
        )
    finally:
        cleanup(root)


def case_equal_and_growing_pass():
    root, ledger, out = stage(n_ledger=5)
    try:
        write_artifact(out, 5)
        equal = g.shrink_guard(5, str(out))[0]
        growing = g.shrink_guard(9, str(out))[0]
        report(
            "equal (5) and growing (9) counts both pass — the common path is not blocked",
            equal is True and growing is True,
            "equal=%s growing=%s" % (equal, growing),
        )
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------- the escape hatch

def case_allow_shrink_writes_and_says_so():
    root, ledger, out = stage(n_ledger=3)
    try:
        write_artifact(out, 166)
        code = g.main(["--ledger", str(ledger), "--out", str(out), "--allow-shrink"])
        report(
            "--allow-shrink writes the smaller artifact and exits 0",
            code == 0 and "%s** 3" % g.COUNT_LABEL in out.read_text(encoding="utf-8"),
            "code=%s" % code,
        )
    finally:
        cleanup(root)


def case_dry_run_never_writes():
    root, ledger, out = stage(n_ledger=3)
    try:
        write_artifact(out, 166)
        before = out.read_text(encoding="utf-8")
        code = g.main(["--ledger", str(ledger), "--out", str(out), "--dry-run"])
        report(
            "--dry-run reports the refusal but leaves the artifact byte-identical, exit 0",
            code == 0 and out.read_text(encoding="utf-8") == before,
            "code=%s changed=%s" % (code, out.read_text(encoding="utf-8") != before),
        )
    finally:
        cleanup(root)


# ------------------------------------------------------------------------- the coupling itself

def case_render_and_guard_agree():
    """The guard reads a number the renderer wrote. Test the round trip, not the regex."""
    root, ledger, out = stage(n_ledger=7)
    try:
        g.main(["--ledger", str(ledger), "--out", str(out)])
        read_back = g.records_claimed_by(str(out))
        report(
            "round trip: what render() writes is exactly what records_claimed_by() reads (7)",
            read_back == 7,
            "read_back=%s\n%s" % (read_back, out.read_text(encoding="utf-8")),
        )
    finally:
        cleanup(root)


def case_count_line_below_the_window_is_not_scanned():
    """Bounded read is deliberate; if the line moves out of the window it fails open."""
    root, ledger, out = stage(n_ledger=1)
    try:
        out.write_text(
            ("filler\n" * 2000) + "**%s** 166\n" % g.COUNT_LABEL,
            encoding="utf-8",
            newline="\n",
        )
        claimed = g.records_claimed_by(str(out))
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "count line pushed past the read window fails OPEN by design (documented limit)",
            claimed is None and code == 0,
            "claimed=%s code=%s" % (claimed, code),
        )
    finally:
        cleanup(root)


def case_missing_artifact_has_no_opinion():
    report(
        "absent artifact returns None, not 0 — 0 would make every first run look like loss",
        g.records_claimed_by(os.path.join(tempfile.gettempdir(), "harness-dwg-nope.md")) is None,
    )


def case_broken_ledger_reads_as_empty_and_is_then_refused():
    """A corrupt ledger must not be silently rendered over a healthy artifact."""
    root, ledger, out = stage(n_ledger=0)
    try:
        ledger.write_text("{not json at all", encoding="utf-8")
        write_artifact(out, 42)
        code = g.main(["--ledger", str(ledger), "--out", str(out)])
        report(
            "corrupt ledger degrades to empty and is then caught by the shrink guard (exit 2)",
            code == 2 and "42" in out.read_text(encoding="utf-8"),
            "code=%s" % code,
        )
    finally:
        cleanup(root)


def main():
    case_refuses_the_shrink()
    case_message_names_the_loss_and_the_state()
    case_empty_state_is_the_dangerous_case()
    case_first_run_is_not_a_shrink()
    case_unreadable_count_fails_open()
    case_equal_and_growing_pass()
    case_allow_shrink_writes_and_says_so()
    case_dry_run_never_writes()
    case_render_and_guard_agree()
    case_count_line_below_the_window_is_not_scanned()
    case_missing_artifact_has_no_opinion()
    case_broken_ledger_reads_as_empty_and_is_then_refused()
    print("\n%s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
