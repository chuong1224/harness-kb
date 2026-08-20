#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
derived_write_guard.py — let a generator refuse to shrink the artifact it owns.

The problem this exists for
---------------------------
Moving a tool into the vault is the right call, and this repo argues for it at length.
What that argument does not mention — and what bit us the day we finished the move — is
that the move *creates* a hazard the tool never had outside.

A generator that lived in one machine's config directory could only ever run there. Put it
in the vault and every machine that syncs the vault can now run it. That is the whole
point. But such a generator usually has two inputs, and only one of them travels:

  * the **derived artifact** it writes — inside the vault, synced, shared, the thing
    everybody reads;
  * its **accumulated state** — a ledger, a cache, a cursor — which is per-machine and by
    the same rule stays *outside* the vault (state is not configuration, but it is exactly
    as machine-local, and putting a write-heavy ledger inside a file-syncing folder buys
    you conflict copies instead of history).

So the tool arrives on the second machine complete and runnable, and its memory arrives
empty. Run it there and it does precisely what it was written to do: render the artifact
from the ledger it can see. Ours would have replaced a 166-row performance log with the
three runs that machine happened to have on disk. No error, no traceback, exit 0, a
cheerful "[OK] wrote note" — and a hundred and sixty-three rows of history gone, in the
one file nobody re-reads because it is generated.

The invariant that saves you
----------------------------
Most accumulating generators have one: **the record set only grows.** Ours unions by
session id and keeps rows whose transcripts were long since cleaned up, so the count can
rise or stay flat, never fall. When an invariant like that exists, the generator can check
its own output before overwriting it:

    the artifact already claims more records than I am about to write
        → I am not the machine that wrote it, or my state was truncated
        → refuse, exit non-zero, change nothing

That is the entire mechanism. It costs one read of the file you are about to clobber, and
it converts the failure from silent data loss into a loud, specific stop.

Three properties worth keeping
------------------------------
1. **Fail closed on shrink, fail open on doubt.** If the artifact is missing, or the count
   line cannot be found, the write proceeds. A guard that blocks whenever it cannot parse
   something turns into an outage the first time you restyle the template — and unlike the
   loss it prevents, an outage is visible. Know which way each branch fails, and say so.
2. **The escape hatch must be explicit and awkward.** `--allow-shrink` exists because
   deliberate truncation is real (you merged ledgers, you rebuilt from scratch). It must
   never be something a scheduled prompt is allowed to add on its own — a routine that
   retries with the override is a routine that has automated away the guard. Say that in
   the routine's own instructions, not only here.
3. **The artifact has to state its own count.** This guard reads a number the generator
   itself wrote on the previous run, which makes the artifact self-describing and the check
   independent of any state the current machine holds. If your template has no such line,
   add one before you add this guard: a footer with the record count is worth having on its
   own merits.

Usage
-----
    python derived_write_guard.py --ledger ledger.json --out log.md
    python derived_write_guard.py --ledger ledger.json --out log.md --dry-run
    python derived_write_guard.py --ledger ledger.json --out log.md --allow-shrink

Exit codes: 0 = written (or dry run) · 2 = refused, shrink detected · 1 = bad input.

The ledger format here is deliberately trivial — a JSON object keyed by record id — because
the point is the guard, not the renderer. Lift `records_claimed_by()` and `shrink_guard()`
into whatever generator you already have.
"""

import argparse
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The generator writes this line; the guard reads it back on the next run. Keep the two
# together — a template change that moves or renames it silently disarms the guard, which
# is why the test suite asserts on the round trip rather than on the regex alone.
COUNT_LABEL = "Records logged:"
COUNT_RE = re.compile(re.escape(COUNT_LABEL) + r"\s*\*{0,2}\s*(\d+)")

# How far into the artifact to look. Bounded so a huge generated file is not read whole
# just to answer "how many rows did you claim last time".
HEAD_BYTES = 4096


def records_claimed_by(artifact_path, head_bytes=HEAD_BYTES):
    """How many records the existing artifact says it holds.

    Returns None when the artifact is absent or does not state a count — both of which
    mean "no opinion", not "zero". Returning 0 here would make every first run look like a
    catastrophic shrink from itself.
    """
    try:
        with open(artifact_path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(head_bytes)
    except OSError:
        return None
    m = COUNT_RE.search(head)
    return int(m.group(1)) if m else None


def shrink_guard(new_count, artifact_path, state_path=None):
    """(ok, message) — refuse when the artifact already claims more than we would write.

    Fails OPEN when there is nothing to compare against (see property 1 in the docstring):
    a missing artifact, or one whose count line cannot be read, is not evidence of loss.
    """
    claimed = records_claimed_by(artifact_path)
    if claimed is None or new_count >= claimed:
        return True, ""
    lines = [
        "REFUSED: %s already reports %d records; this run would write %d, losing %d."
        % (artifact_path, claimed, new_count, claimed - new_count),
        "  The record set only grows, so a smaller number means this run is not reading",
        "  the state that produced the file — most likely a machine whose local state is",
        "  empty or behind.",
    ]
    if state_path:
        lines.append("  State read from: %s" % state_path)
    lines.append(
        "  If the shrink is intended (ledgers merged, history rebuilt), re-run with"
        " --allow-shrink."
    )
    return False, "\n".join(lines)


def load_ledger(path):
    """The accumulating, per-machine state. Missing or unreadable reads as empty."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def render(ledger):
    """Trivial renderer — stands in for whatever your generator actually produces.

    Note the count line: the guard on the *next* run reads this number back.
    """
    rows = sorted(ledger.items())
    body = "\n".join("| `%s` | %s |" % (rid, rec) for rid, rec in rows) or "| _none_ | |"
    return (
        "# Derived log\n\n"
        "> Generated file — rewritten in full on every run. Do not hand-edit.\n\n"
        "**%s** %d\n\n"
        "| Record | Value |\n"
        "|---|---|\n"
        "%s\n" % (COUNT_LABEL, len(rows), body)
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render a derived artifact from per-machine state, refusing to shrink it."
    )
    ap.add_argument("--ledger", required=True, help="per-machine accumulating state (JSON)")
    ap.add_argument("--out", required=True, help="derived artifact to write")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument(
        "--allow-shrink",
        action="store_true",
        help="write even though the artifact claims more records (deliberate truncation only)",
    )
    args = ap.parse_args(argv)

    ledger = load_ledger(args.ledger)
    text = render(ledger)
    ok, message = shrink_guard(len(ledger), args.out, state_path=args.ledger)

    if args.dry_run:
        print("[DRY-RUN] %d records would be written to %s" % (len(ledger), args.out))
        if not ok:
            print(message)
        return 0

    if not ok and not args.allow_shrink:
        print(message)
        return 2

    if not ok:
        print("[WARN] shrink accepted by --allow-shrink: %d records written." % len(ledger))

    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("[OK] wrote %s (%d records)" % (args.out, len(ledger)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
