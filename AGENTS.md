# Working on this repo

Repo-local instructions for AI coding agents and humans editing Harness KB. This file has
authority over **this repository only**. It is not the policy template for a knowledge base
created by the scaffold; `scaffold/AGENTS.md` is that user-owned starting point.

`CLAUDE.md` is deliberately only a pointer to this file. Keep one multi-agent source of truth.

## What this is

A blueprint, reference implementations, and a lifecycle-safe scaffold for a self-maintaining
knowledge base. `examples/scripts/harness.py init` installs the working parts inside a target
vault, because a machine holding the notes but not the tools cannot verify or regenerate them.
The installer must never copy `examples/demo-vault`, repository history, private data, or
repo-maintainer policy into the target.

**Standard library only, with exactly one documented exception.** Every lifecycle and example
script must run under a plain `python script.py` on a clean machine. Do not add a package, a
framework, or a config format that needs one.

The single exception is **PyYAML**, used by `verify_kb.py` to decide whether a note's frontmatter
is valid YAML at all. When PyYAML is absent, `verify_kb.py` exits `2` and refuses to certify the
vault. Extending the exception, or letting a missing dependency degrade quietly into a pass,
needs an explicit changelog decision.

## Scaffold and lifecycle contract

- `scaffold/release.json` is the source manifest for installable components. Its version must
  equal the README badge and newest changelog entry.
- Every installed component records source path, target path, ownership, base hash, installed
  version, and exact source commit in `.harness/manifest.json`.
- `user` components are created once, then never overwritten or recreated by an upgrade.
- `upstream` components may update only when their current bytes still equal the recorded base.
  A local edit is a conflict, not permission to overwrite it.
- Network update checks require stored consent, use a per-vault cache, and stay non-blocking and
  quiet when offline.
- Upgrade is always `plan` then `apply`; apply backs up every touched path, runs the new gates,
  automatically restores on red, and leaves an explicit manual rollback handle.
- All lifecycle state lives under the target vault. Two vaults on one host must never share a
  cache, plan, backup, baseline, ledger, or identity by default.

## Before you commit

```bash
# both gates, pointed at the fixture vault
python examples/scripts/verify_kb.py examples/demo-vault --rules examples/rules/rules.example.json
python examples/scripts/check_rules_drift.py examples/demo-vault --rules examples/rules/rules.example.json

# the break-the-gate suites that guard the tooling itself
python examples/scripts/test_verify_kb.py
python examples/scripts/test_drift_check.py
python examples/scripts/test_claim.py
python examples/scripts/test_routine_guard.py
python examples/scripts/test_tooling_selfcheck.py
python examples/scripts/test_auto_fix.py
python examples/scripts/test_worklist.py
python examples/scripts/test_audit_gate.py
python examples/scripts/test_derived_write_guard.py
python examples/scripts/test_harness.py
python examples/scripts/test_release_metadata.py
```

Both gates must exit `0` against `examples/demo-vault`: zero errors from the integrity gate and
zero drift from the rules checker. Expected warnings do not fail a gate; errors and drift do.

Read an exit code **directly**, never through a pipe. `python verify_kb.py ... | tail -3` reports
the exit code of `tail`, so a failing gate can look green.

## Every tool ships with the test that breaks it

Each script has a `test_<name>.py` beside it, and those tests try to defeat the guard rather than
only confirm the happy path. A lifecycle test must exercise two vaults, user customization,
source/target conflict, cache/offline behavior, a fake newer release, gate failure, and rollback.

## Release rules

- **SemVer.** The README badge, newest `CHANGELOG.md` entry, and `scaffold/release.json` version
  move together in one commit.
- `CHANGELOG.md` follows Keep a Changelog and every entry has one canonical release-link footer.
- Tag each release `vX.Y.Z` as an **annotated** tag.
- Published tags are never amended or force-pushed; a mistake ships as a new PATCH.
- Commit messages and documentation are English. Keep README English and Vietnamese claims in
  step when behavior changes.
- The blueprint's section numbers are referenced by README and examples. Do not renumber them
  for cosmetic reasons.
- Before publishing, scan both file names and contents for private names, credentials, machine
  paths, internal task IDs, and internal data.
