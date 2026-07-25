# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.2.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.2.0
[1.1.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.1.0
[1.0.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.0.0
