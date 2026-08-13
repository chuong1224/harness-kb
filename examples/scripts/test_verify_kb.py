#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_verify_kb.py - break-the-gate tests for verify_kb.py.

Every case builds a throwaway vault under the system temp directory, so the suite never
reads or writes a real knowledge base.

A gate that is always green is worse than no gate: it hands out confidence nobody
earned. So these cases push from both sides - the gate must SHOUT at things that are
genuinely broken, and must stay SILENT on the valid shapes an earlier version reported
falsely. The bulk of the file targets frontmatter validity, because that is where this
gate was found lying:

  * `summary: "patched the "blank page" icon"` - a double-quoted scalar containing bare
    double quotes. The hand-rolled parser in verify_kb.py returns a plausible string;
    a real YAML parser raises, and so does Obsidian, which means the note silently has
    no title, no tags and no summary. Two notes in the vault this repo is distilled
    from sat broken for days behind a green gate exactly like this.
  * The mirror-image case: the same quotes nested the correct way must NOT be reported.
    A checker that cries wolf on valid notes gets switched off, and then it protects
    nothing.
  * PyYAML missing must produce exit 2, never exit 0. "No problems found" while a
    mandatory checker is absent is not a clean bill of health, it is an unknown one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass

HERE = Path(__file__).resolve().parent
GATE = HERE / "verify_kb.py"

fails: list = []


def check(name: str, cond: bool, info: str = "") -> None:
    if not cond:
        fails.append(name)
    print("  %s %-64s %s" % ("PASS" if cond else "FAIL", name, info if not cond else ""))


def gist(out: str) -> str:
    """The gate's verdict lines, flattened - a failing case has to be readable at a glance."""
    keep = [ln.strip() for ln in out.splitlines()
            if ln.strip().startswith(("[ERR]", "[WARN]", "[OFF]", "RESULT:", "clean -", "error:"))]
    return " | ".join(keep[:4]) or out.strip().replace("\n", " ")[-160:]


def build_vault(notes: dict) -> Path:
    """A vault holding exactly the notes given as {relative path: text}."""
    root = Path(tempfile.mkdtemp(prefix="harness-kb-verify-"))
    for rel, text in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def run(vault: Path, env=None):
    p = subprocess.run([sys.executable, "-B", str(GATE), str(vault)],
                       capture_output=True, timeout=120, env=env)
    dec = lambda b: (b or b"").decode("utf-8", "replace")       # noqa: E731
    return p.returncode, dec(p.stdout) + dec(p.stderr)


def env_without_pyyaml():
    """An environment where `import yaml` genuinely raises.

    A stub module that raises on import is placed first on PYTHONPATH, so the gate takes
    the same branch it would take on a machine that never installed PyYAML. Monkey
    patching the gate's internals instead would only prove the test can edit variables.
    """
    shadow = Path(tempfile.mkdtemp(prefix="harness-kb-noyaml-"))
    (shadow / "yaml.py").write_text(
        "raise ImportError('PyYAML withheld by the test suite')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(shadow) + os.pathsep + env.get("PYTHONPATH", "")
    return env, shadow


VALID = ('---\ntitle: Valid\naliases: ["v"]\nsummary: "a plain summary"\ntags: [note]\n'
         '---\n\n# Valid\n\nBody text.\n')

# The exact shape that hid behind a green gate: double quotes nested in double quotes.
NESTED_QUOTES = ('---\ntitle: Broken\naliases: ["b"]\n'
                 'summary: "patched the "blank page" icon today"\ntags: [note]\n'
                 '---\n\n# Broken\n\nBody text.\n')

temps: list = []


def vault(notes: dict) -> Path:
    v = build_vault(notes)
    temps.append(v)
    return v


print("verify_kb.py - break-the-gate suite\n")

# --- baseline: a valid vault must be silent, or nothing below means anything ---
rc, out = run(vault({"Valid.md": VALID}))
check("valid vault -> clean, exit 0", rc == 0 and "clean - 0 problems" in out, gist(out))

# --- the finding this check exists for ---
rc, out = run(vault({"Broken.md": NESTED_QUOTES}))
check("nested double quotes -> reported, exit 1",
      rc == 1 and "invalid YAML frontmatter" in out, gist(out))
check("report names the line and column, so it is actionable",
      "line 4, column" in out, gist(out))

# Quoted the correct way round: valid YAML, and the gate must not invent a problem.
rc, out = run(vault({"Fine.md": (
    "---\ntitle: Fine\naliases: [\"f\"]\n"
    "summary: 'patched the \"blank page\" icon today'\ntags: [note]\n"
    "---\n\n# Fine\n\nBody text.\n")}))
check("single-quoted scalar holding double quotes -> valid, stays silent",
      rc == 0 and "invalid YAML" not in out, gist(out))

# A tab where YAML demands spaces - invisible in an editor, fatal to the parser.
rc, out = run(vault({"Tab.md": (
    "---\ntitle: Tab\naliases: [\"t\"]\nsummary: \"s\"\ntags:\n\t- note\n"
    "---\n\n# Tab\n\nBody text.\n")}))
check("tab used for indentation -> reported", rc == 1 and "invalid YAML" in out,
      gist(out))

# Parses, but into a list. Every meta["title"] lookup downstream would be fiction.
rc, out = run(vault({"NotMap.md": "---\n- just\n- a list\n---\n\n# NotMap\n\nBody.\n"}))
check("frontmatter that is not a mapping -> reported",
      rc == 1 and "must be a mapping" in out, gist(out))

# A broken note must yield ONE finding, not a cascade of derived ones. Noise buries
# the actual cause and trains people to skim the output.
rc, out = run(vault({"Broken.md": NESTED_QUOTES}))
check("broken YAML suppresses the derived frontmatter findings for that note",
      out.count("[ERR]") == 1 and "missing required frontmatter" not in out,
      gist(out))

# --- valid shapes that must not be dragged in ---
rc, out = run(vault({"NoFm.md": "# NoFm\n\nA note with no frontmatter at all.\n"}))
check("note without frontmatter -> not a YAML error", "invalid YAML" not in out,
      gist(out))

rc, out = run(vault({"Rule.md": "---\n\nnot frontmatter, just a horizontal rule\n"}))
check("leading horizontal rule -> not mistaken for frontmatter",
      "invalid YAML" not in out, gist(out))

rc, out = run(vault({"Skip.md": ('---\ntitle: Skip\ngate_ignore: true\n'
                                 'summary: "broken "quotes" here"\n---\n\n# Skip\n\nBody.\n')}))
check("gate_ignore note -> exempt from this check too, like every other check",
      rc == 0 and "invalid YAML" not in out, gist(out))

# --- the checker itself going missing must not read as good news ---
env, shadow = env_without_pyyaml()

rc, out = run(vault({"Valid.md": VALID}), env=env)
check("PyYAML missing on a CLEAN vault -> exit 2, not 0",
      rc == 2 and "DEGRADED" in out, gist(out))
check("missing checker is named, with the fix", "pip install pyyaml" in out,
      gist(out))

rc, out = run(vault({"Broken.md": NESTED_QUOTES}), env=env)
check("PyYAML missing on a BROKEN vault -> still exit 2, never a silent pass",
      rc == 2, gist(out))

shutil.rmtree(shadow, ignore_errors=True)
for t in temps:
    shutil.rmtree(t, ignore_errors=True)

print("\n%s - %d case(s) failed" % ("FAIL" if fails else "ALL PASS", len(fails)))
sys.exit(1 if fails else 0)
