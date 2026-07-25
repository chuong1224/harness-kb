#!/usr/bin/env python3
"""
test_drift_check.py - Prove that check_rules_drift.py actually catches drift.

A gate you never tried to break is a gate you do not know works. Each case copies the demo
vault plus the rules file into a temporary directory, breaks exactly one thing there, and
asserts the checker exits 1 with the right message. The repository working tree is never
modified, so an interrupted run cannot leave anything half-broken.

Run from the repository root:
    python examples/scripts/test_drift_check.py

Exit code 0 = every case passed, 1 = at least one case failed.
Zero dependencies: Python 3.8+, standard library only.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "examples" / "scripts" / "check_rules_drift.py"
DEMO = REPO / "examples" / "demo-vault"
RULES = REPO / "examples" / "rules" / "rules.example.json"
RULES_DOC = "Ops/Vault Rules/Vault Rules.md"

failures = 0


def run(vault: Path, rules: Path):
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(vault), "--rules", str(rules)],
        capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def report(name, ok, output=""):
    global failures
    if not ok:
        failures += 1
        print(f"[FAIL] {name}\n---- output ----\n{output}")
    else:
        print(f"[PASS] {name}")


def sandbox(tmp: Path):
    """A throwaway copy of the demo vault and the rules file."""
    vault = tmp / "vault"
    rules = tmp / "rules.json"
    shutil.copytree(DEMO, vault)
    shutil.copy2(RULES, rules)
    return vault, rules


def case(name, mutate, expected):
    """mutate(vault, rules) breaks one thing; the checker must exit 1 and say `expected`."""
    with tempfile.TemporaryDirectory() as tmp:
        vault, rules = sandbox(Path(tmp))
        mutate(vault, rules)
        code, out = run(vault, rules)
        report(name, code == 1 and expected in out, out)


def replace_in(path: Path, old: str, new: str):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"anchor text not found in {path.name}: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        vault, rules = sandbox(Path(tmp))
        code, out = run(vault, rules)
        report("baseline: unmodified demo vault has no drift", code == 0, out)

    case(
        "document states the wrong tag count",
        lambda v, r: replace_in(v / RULES_DOC, "(7 tags)", "(6 tags)"),
        "DRIFT 'tag_count'",
    )
    case(
        "a tag is added to the vocabulary but documents have not caught up",
        lambda v, r: replace_in(r, '"reference", "index"', '"reference", "index", "meeting"'),
        "enumeration is incomplete, missing meeting",
    )
    case(
        "a marker is deleted from a registered document",
        lambda v, r: replace_in(v / RULES_DOC, "<!-- rules:index_count -->", ""),
        "marker 'index_count' is gone",
    )
    case(
        "the filesystem changes under a hard-coded count (an index is removed)",
        lambda v, r: (v / "Ops" / "Index - Ops.md").unlink(),
        "DRIFT 'index_count'",
    )
    case(
        "a registered document goes missing",
        lambda v, r: (v / RULES_DOC).unlink(),
        "registered as a consumer but the file is missing",
    )

    print(f"\n{'all cases passed' if failures == 0 else str(failures) + ' case(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
