# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - 2026-07-28

### Added
- `examples/scripts/auto_fix.py`: the **correction stage** of the loop — roadmap item **H2**, and the
  first script here that writes to your notes. It opens exactly one class: a number on a line
  carrying a marker, where the marker names the claim and the rules file supplies the value, so the
  fixer never has to understand the sentence it edits. Two independent sources must agree (the
  checker's `X -> Y` and the fixer's own re-read of that line) or the fix is skipped with a reason;
  every touched file is backed up first; both gates re-run afterwards and a red one restores the
  whole run and exits non-zero. Incomplete enumerations, removed markers and anything semantic stay
  in the report on purpose.
- `examples/scripts/test_auto_fix.py`: 9 break-the-fixer cases — a dry run that writes nothing, a
  fix that lands on exactly the original bytes, a second run that is a no-op, a number spelled out
  in words that must be skipped rather than guessed, files outside the rules registry left
  untouched, manual rollback, and the one that makes the rest believable: a forced red gate after
  the fix, which must roll everything back and exit 1.
- `examples/routines/kb-autofix-daily.SKILL.md`: template for the scheduled run that follows the
  read-only audit — exit-code handling per case, a single-file write boundary, and the log
  discipline (copy numbers verbatim, never claim a task is done unverified, log clean days too).

### Changed
- Blueprint H2 gained its reference-implementation section: why one class first, why two sources
  must agree, why the gate decides whether a fix counts, and why the refusals are part of the
  contract rather than gaps. Acceptance criterion "at least one class of mechanical error is
  auto-fixed" is now ticked, with the rollback path exercised by a test rather than asserted.
- README documents the fixer as the deliberately narrowest script in the repo, and pairs it with
  the per-file lock: a file another stream holds defers the run instead of racing it.

## [1.5.0] - 2026-07-26

### Added
- `examples/scripts/tooling_selfcheck.py`: **the gate that runs the gates**. Once the checkers live
  inside the vault, their test suites need someone to run them — and "whoever remembers" is
  documentation, not a gate. This runner **discovers** every `*/attachments/test_*.py` (no
  hand-maintained list, which is the same failure mode one level up) and a `Stop` hook calls it at
  the end of each turn: nothing changed → it exits in ~0.2s; tooling changed and a suite is red →
  exit 2, blocking the turn until it is fixed.
- `examples/scripts/test_tooling_selfcheck.py`: 20 break-the-gate cases — discovery scope, real
  execution, the stale marker, timeouts, the `Stop` hook blocking and refusing to block twice, the
  kill switch, and the one that matters most: a mistyped `--vault` must not report a false green.
- Hook example gained the `Stop` entry, and `examples/hooks/README.md` now documents all three legs
  it wires: observability (logging), coordination (the claim lock), verification (the tooling gate).

### Changed
- Blueprint §5 gained **"A gate nobody runs is not a gate"**: why an unrun suite fails silently
  rather than loudly, the four properties that make an automatic gate survivable (discover the
  suite, do nothing when nothing changed, never record success on a red run, fail open and never
  trap a session), and the false-green bug that made the last one concrete — the dangerous failure
  mode of a gate is not a false alarm, it is quiet reassurance.

## [1.4.0] - 2026-07-26

### Added
- `examples/scripts/claim.py`: a **mechanical per-file lock** for knowledge bases that more than one
  agent session writes to — the reference implementation of roadmap item **H4**. A `PreToolUse` hook
  claims a file before the write and exits 2 (blocking the tool call) when another live stream holds
  it, with a message naming the holder and the ways out. Free files are claimed silently, so a
  single agent never notices the lock.
- `examples/scripts/test_claim.py`: 25 break-the-lock cases proving it blocks when it should
  (contention, hook path, role reversal after a takeover, close races) and stays out of the way when
  it shouldn't (renewals, other files, read tools, its own state directory, files outside the vault,
  malformed payloads, no session id, the kill switch).
- Hook example gained the `PreToolUse` and `SessionEnd` entries, and `examples/hooks/README.md` now
  documents both legs — logging (async, cannot deny) and the claim lock (synchronous, blocking) —
  including a copy-pasteable way to watch the lock refuse a write before trusting it in a session.

### Changed
- Blueprint §6 H4 is no longer a sketch: it states the four decisions the implementation rests on
  (one claim file per stream so a synced folder cannot produce conflict copies; the earliest claim
  wins so conflicts resolve from data rather than write order; silence when working alone; fail
  open), and the two limits worth saying out loud — cross-machine safety is bounded by file-sync
  latency, and only the agent's file tools pass through the hook.

## [1.3.0] - 2026-07-25

### Added
- Blueprint §5: **"The tooling has to live in the vault, not beside it"** - why generators and
  checkers kept in per-machine config break self-sufficiency, become a drift source of their own,
  and land in the audit's blind spot; what may legitimately stay outside (secrets, large binaries,
  per-machine paths); the scaling argument for teams; the clean-machine test; and the migration
  caveat that a ported generator must match the previous output field for field.
- `generate_catalog.py --check`: writes nothing and exits 1 when the catalog on disk no longer
  matches the vault, so a stale catalog fails a scheduled job instead of silently degrading
  retrieval. Detection stays separate from correction.
- Daily audit routine gained a step that runs the catalog gate, and treats "the script is missing on
  this machine" as the finding itself.
- `.gitattributes` pinning `eol=lf`, so a clone on a machine with `core.autocrlf=true` cannot
  silently rewrite every line — the same per-machine drift the blueprint argues against, applied
  to this repository itself.

## [1.2.1] - 2026-07-25

### Changed
- Provenance lines in the README and blueprint now date the snapshot to the release that
  introduced the drift checker, so the "reflects a real working system as of" claim is accurate.

## [1.2.0] - 2026-07-25

### Added
- `check_rules_drift.py`: a working implementation of H1's detection half. Documents keep the
  numbers readers want, but each restating line carries a marker comment
  (`<!-- rules:tag_count -->`); the checker compares every marked claim with the single source of
  truth and exits 1 with a `file:line` on drift. Deleting a marker is itself an error, so drift
  cannot hide by removing the evidence.
- `test_drift_check.py`: break-the-gate tests — six cases (wrong count, vocabulary extended,
  marker deleted, index removed from the filesystem, registered document missing) run against a
  temporary copy of the demo vault, so the working tree is never modified.
- `rules.example.json`: a `documents` block — marker prefix, claim types with their read patterns,
  the registry of consuming documents, retired tags, and noise-control heuristics for the sweep
  over unmarked numbers.
- `demo-vault`: an `Ops/Vault Rules` note that restates the vocabulary, areas and index count with
  markers, so the drift checker runs against something real out of the box.

### Changed
- Blueprint section 5 now covers the marker pattern explicitly: documents may *show* the numbers,
  they may not *own* them — plus why detection stays separate from correction (the H2 precondition).
- H1 in the roadmap is marked as having a reference implementation, not just a recommendation.
- README: quickstart gained the drift check and an explanation of the demo's expected warnings.
- Daily-audit routine template now runs the drift check as a command instead of asking the agent to
  compare rule documents by eye, and tells it to register new claims rather than re-report them.

## [1.1.0] - 2026-07-24

### Added
- `verify_kb.py`: broken-anchor check for `[[Note#Heading]]` wikilinks.
- Provenance line in the README and blueprint that dates the snapshot.
- This changelog and a version badge — the project now follows explicit release versioning.

### Changed
- Blueprint kept evergreen: the tag/index drift figures are now framed as illustrative
  examples, and the scorecard no longer states pegged percentages.
- Example hook matcher now includes `NotebookEdit`, matching a complete edit-tool set.

## [1.0.0] - 2026-07-24

### Added
- Initial release: the self-operating knowledge-base "harness" blueprint (`docs/blueprint.md`)
  — closed-loop definition, 7-property scorecard, loop diagnosis, roadmap, and acceptance criteria.
- Reference artifacts (`examples/`): single-source-of-truth rules, an integrity gate
  (`verify_kb.py`), a triage catalog generator, an activity-logging hook, a daily-audit
  routine template, and a runnable demo vault.
- MIT license.

[1.2.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.2.1
[1.2.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.2.0
[1.1.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.1.0
[1.0.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.0.0
