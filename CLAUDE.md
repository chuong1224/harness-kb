# Working on this repo

Repo-local notes for AI coding agents — and humans — editing this repository. This file has
authority over **this repo only**. It is not a template for the rules of the knowledge base
you are building; the blueprint is.

## What this is

A blueprint plus reference implementations for a self-maintaining knowledge base. The
scripts under `examples/scripts/` are meant to be **copied into the vault they serve**, not
run from a checkout of this repo — a machine holding the notes but not the tools cannot
verify or regenerate anything.

**Zero dependencies, standard library only.** Every script must run under a plain
`python script.py` on a clean machine. Do not add a package, a framework, or a config
format that needs one.

## Before you commit

```bash
# both gates, pointed at the fixture vault
python examples/scripts/verify_kb.py examples/demo-vault --rules examples/rules/rules.example.json
python examples/scripts/check_rules_drift.py examples/demo-vault --rules examples/rules/rules.example.json

# the break-the-gate suites that guard the tooling itself
python examples/scripts/test_drift_check.py
python examples/scripts/test_claim.py
python examples/scripts/test_routine_guard.py
python examples/scripts/test_tooling_selfcheck.py
python examples/scripts/test_auto_fix.py
python examples/scripts/test_worklist.py
python examples/scripts/test_audit_gate.py
```

Both gates must exit `0` against `examples/demo-vault`: zero errors from the integrity gate,
zero drift from the rules checker. The drift checker also prints warnings about areas and tags
the example rules declare but the six-note demo never uses — that is deliberate, and it is
exactly the signal you want when a vocabulary entry falls out of use. Warnings do not fail a
gate; errors and drift do.

Read an exit code **directly**, never through a pipe: `python verify_kb.py … | tail -3`
reports the exit code of `tail`, which almost always succeeds, so a failing gate still looks
green.

## Every tool ships with the test that breaks it

Each script here has a `test_<name>.py` beside it, and those tests try to *defeat* the
guard rather than confirm it works on the happy path. Adding a script without its paired
suite is incomplete work — a gate nobody can break is a gate nobody has tested.

## Release rules

- **SemVer.** The version badge at the top of `README.md` must equal the newest entry in
  `CHANGELOG.md`. They move in the same commit, never apart.
- `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Tag each release `vX.Y.Z` as an **annotated** tag.
- **Published tags are never amended or force-pushed.** A mistake ships as a new PATCH.
- Commit messages and documentation in English. The README carries a Vietnamese section;
  keep both sides in step when you change what the repo claims to do.
- The blueprint's section numbers are referenced from the README and from the examples. Do
  not renumber them for cosmetic reasons.
