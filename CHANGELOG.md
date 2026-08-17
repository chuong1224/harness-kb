# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.15.0] - 2026-08-18

### Added
- **Blueprint: point a gate at its own source of truth, not only at the world it describes.** The
  section on parsing already said to let the real parser render the verdict. That closes the
  "almost right regex" hole and not the one behind it: real parsers are lenient by specification,
  and every check you own may be aimed somewhere else entirely. Our registry gate compared one JSON
  file against the vault in both directions for months without ever inspecting the file itself, and
  two failures used the gap — an object carrying the same key twice, which `json.loads` resolves
  silently to the last one, and a whole-file reformat that the format-preserving writer was about
  to make permanent. Three consequences: check the shape of the source of truth in the same gate
  (duplicate keys at every level, the formatting contract, records marked finished with no finish
  date); never store the contract inside the artifact it governs, because one bad write can flip
  the file and its judge together; and put the check ahead of the next write, because this class
  erases its own evidence — both symptoms were gone before anyone looked, having never reached a
  commit. The section closes on what did catch it, which was not a gate but an acceptance identity:
  the byte delta of an edit must equal the bytes you meant to add. Gates catch the failure classes
  someone imagined; keep one plain identity over the artifact for the classes nobody has.

## [1.14.1] - 2026-08-17

### Changed
- **Blueprint: a verdict a gate states as fact must be re-derived, not parsed out of the failure.**
  The "distinguish broken code from broken measurement" corollary had a trap inside it that we
  walked into. Once that distinction earns its own verdict, the tempting way to detect it is to read
  the error text — but a fixture can print any string, and the same exception arrives from causes the
  label does not cover. Ours fires when a spreadsheet is open in a desktop editor; the identical
  error also comes from ordinary filesystem permissions. A text-inferred label eventually lands on
  the wrong failure, and then the gate confidently tells people to close an application that was
  never open — worse than the blunt "it is red" it replaced, because it sounds specific. The section
  now says to take the measurement again at classification time, and adds the two constraints that
  keep such a verdict honest: it must block exactly as hard as a red suite and record no green mark,
  and its trigger must stay narrow enough that unrelated failures cannot borrow it.

## [1.14.0] - 2026-08-16

### Added
- **A fifth property for the end-of-turn gate: measure what the suite did, do not ask it.** The
  section previously stopped at four, and all four are about *running* the suites. None of them
  notices when a suite still runs but has stopped checking anything. A zero exit proves nothing was
  raised, not that anything was verified — a suite that skips quietly (`if not condition: pass`), or
  that loses assertions to a careless edit, still exits zero while the gate stamps another green run
  over coverage that has evaporated. The property: have each suite print one line per assertion,
  count those lines, compare against the last green run, and treat a drop as a fact rather than an
  intent to be guessed. Deleting five cases and silently skipping five look identical from outside.
  It also names the necessary exit — a way to lower the mark *with a stated reason*, because a gate
  with no legitimate way out is a gate that gets switched off.
- **The blind spot that measurement creates, stated as a general rule.** Counting assertions catches
  a suite that *goes* quiet; it cannot see a suite that was never audible. Three of ours printed no
  assertion line at all — they used a test framework whose own spelling the runner did not read — so
  their count was zero, and zero cannot fall: an entire test class could be deleted from any of them
  without moving a number. Hence the rule that generalizes past any one runner — **a gate built on
  deltas must also refuse the value that has no delta** — and the reason it gets no acceptance path,
  unlike a coverage drop: lowering a mark concedes that coverage fell for a reason, while silence
  means the instrument never reached the suite at all.
- **Two corollaries about the instrument itself.** Separate *the code is broken* from *the
  measurement is broken*, so a suite that is red for a missing library is not reported as a coverage
  problem and nobody is sent to edit healthy code. And make the instrument deterministic before
  trusting its numbers: ours decoded child output as UTF-8 while the child wrote in the platform's
  locale encoding, so one separator came back mangled, one counting pattern missed, and a suite
  scored 10 or 1 depending on which shell had launched the runner. A suite that only passes on one
  operator's machine has not been measured.

## [1.13.1] - 2026-08-14

### Fixed
- **The two suites that delete test directories now require proof that the target belongs to
  the current run.** `test_claim.py` and `test_tooling_selfcheck.py` no longer initialize a
  vault path as `Path()` (the current directory). Every recursive deletion goes through
  `own_temp()` and `wipe()`, which accept only the exact `mkdtemp` root created by this process
  or one of its descendants, under the system temp directory and with the suite's prefix.
  This closes the same failure pattern that once turned an uninitialized test path into a
  recursive deletion of the real working directory.
- Each suite adds four break-the-fuse checks: reject the current directory, reject the system
  temp root, accept this run's root and descendants, and reject a same-prefix directory owned
  by another run. Mutation testing made `own_temp()` always return true; both suites exited 1
  and all three rejection checks failed, proving the new cases depend on the fuse.

## [1.13.0] - 2026-08-13

### Added
- **The integrity gate now validates frontmatter with a real YAML parser, and admits when it
  cannot.** `verify_kb.py` previously read note metadata only through its own lenient regex
  parser, which returns a plausible string for `summary: "the "blank page" icon"` — while
  Obsidian and every spec-compliant reader see a note with no title, no tags and no summary.
  The gate compared its own lenient reading against itself, found no contradiction, and
  printed green; in the system this repo is distilled from, two notes stayed broken that way
  for days, with the daily audit agreeing every morning because it made the same assumption.
  Validity is now decided by `yaml.safe_load` and reported as `file, line, column` so it can
  be jumped to. A note whose YAML is broken yields exactly one finding: the derived
  frontmatter checks are suppressed for it, because they read metadata that is fiction.
- `examples/scripts/test_verify_kb.py` — the break-the-gate suite this script never had. 13
  cases covering the nested-quote finding, the mirror-image valid form that must stay silent,
  tabs, non-mapping frontmatter, notes without frontmatter, a leading horizontal rule,
  `gate_ignore`, and the missing-dependency path. Verified by mutation: with the new check
  removed, 8 of the 13 fail and the broken fixture reports `clean - 0 problems`.

### Changed
- **`verify_kb.py` exit codes now distinguish "nothing found" from "could not look".** `2` means
  the run cannot certify anything — bad arguments, or a required checker unavailable. When
  PyYAML is missing the gate names it, points at `pip install pyyaml`, and exits `2` instead of
  skipping the check and printing a green line. A gate that cannot be honest about its own
  blind spots has no standing to certify anything else.
- **Dependency policy: standard library, plus exactly one documented exception.** PyYAML is now
  required by `verify_kb.py` alone; every other script remains stdlib-only. The README badge,
  the README quickstart, and `CLAUDE.md` state the exception and the rule attached to it — a
  missing dependency may never degrade quietly into a pass.
- `docs/blueprint.md` gains "Parse the format with a real parser, or your gate will lie about
  it", covering the shared-wrong-assumption failure: two checkers agreeing does not mean two
  independent measurements.

## [1.12.1] - 2026-08-13

### Fixed
- Every release entry now has its Keep a Changelog reference link. The footer had stopped at
  `1.2.1`, leaving 17 published versions from `1.3.0` through `1.12.0` as bracketed headings that
  looked like links but could not be opened. This release adds the full historical set and its own
  `1.12.1` link in newest-first order.

### Added
- `examples/scripts/test_release_metadata.py` — a zero-dependency release gate that keeps the
  README badge, newest changelog entry, and release-link definitions in lockstep. It also rejects
  duplicate entries or links, non-canonical tag URLs, orphan links, and version history that is not
  newest first.

## [1.12.0] - 2026-08-12

### Added
- **H4c reference implementation: "finish with a clean audit" becomes a rule the runtime enforces.**
  `examples/scripts/audit_gate.py` runs every checker you declare and, from a `Stop` hook, refuses
  to end the turn while something is broken. Gates are declared in JSON
  (`examples/rules/gates.example.json`), not hardcoded: each entry names a command, a parser that
  turns its output into stable finding keys (`json:<field>`, `json:checks[].list`, `lines:<regex>`,
  `exit`), and optional `paths` globs so a slow suite stays off the critical path until its files
  change. A gate that exits non-zero but yields no parsed key still produces one finding, so a
  checker whose output format drifts can never be silently downgraded to "clean".

  **The design problem this solves is not blocking — it is not blocking too much.** A guard that
  jails a session over a red lamp somebody else lit is switched off within the week. So the gate
  compares the *set of findings* against a stored baseline and blocks only on findings that are
  new. Inherited findings are printed on every run but never block, and one that genuinely is not
  yours is adopted with `accept --why "…"` — a recorded reason, not a disabled guard. Three
  guarantees keep a session out of jail: `stop_hook_active` means it blocks at most once per turn,
  every ambiguous case fails open (no vault, unreadable payload, missing command, unexpected
  exception), and `AUDIT_GATE_OFF=1` stops it outright.

  The quiet path is a vault fingerprint, so a turn that changed nothing starts no subprocess. The
  baseline lives per machine, outside the vault, for the same reason the other state files do: a
  cloud-synced folder turns a shared state file into a conflict-copy generator.

- `examples/scripts/test_audit_gate.py` — break-the-gate tests on a throwaway vault, aimed at the
  ways the guard could be *harmful*: that inherited debt does not block, that blocking does not
  write the baseline (which would grandfather the finding it just blocked on), that a failing gate
  with unreadable output still counts as failing, that a mistyped `--vault` reports an error instead
  of reporting "clean", and that the shipped default config actually runs this repo's own checkers.

- `examples/demo-vault/.kb-root` — the demo vault now carries a root marker, so the tools that
  refuse to run outside a vault root can be pointed at it. Without it the example gate config could
  not run its third gate against the fixture it ships with.

## [1.11.2] - 2026-08-12

### Added
- `CLAUDE.md` at the repo root — repo-local working notes for coding agents: which gates to run
  before committing, why an exit code must be read without a pipe, the rule that every script
  ships with the test that tries to break it, and the release rules (badge equals the newest
  changelog entry, annotated tags, published tags never amended). A session opened directly in a
  clone of this repository previously loaded no working rules at all.

  Scoped to this repository on purpose. It is **not** a copy of the maintainer's vault rules, so
  it cannot drift into a second source of truth for them and cannot point an agent at tooling
  that does not exist here. It also ships **no** agent hooks or `.claude/` configuration: a
  reference repository should not hand out settings that run commands on a cloner's machine.

## [1.11.1] - 2026-08-09

### Fixed
- The integrity gate now ignores wikilinks, embeds, and headings shown inside inline code or
  fenced code blocks. Documentation examples no longer become false broken-link reports or hide
  genuinely orphaned media. A hostile fixture in the clean demo vault covers single- and
  multi-backtick inline spans plus a four-backtick fence containing a shorter fence; prose after
  the fence is still scanned normally.

## [1.11.0] - 2026-08-09

### Added
- **H3 reference implementation: retrieval signals now become a reviewable worklist.**
  `examples/scripts/worklist.py` consumes one already-measured insight snapshot and emits
  deterministic proposals with a stable `H3-*` ID, priority, exact source/target, and the evidence
  that triggered each action. It covers unseen or unindexed notes, repeated reads, long retrieval
  routes, and high-margin scope-leakage candidates.
- The safety boundary is machine-readable: every result is `proposal_only`, `auto_apply` is always
  false, and every item requires review. The generator does not recompute sensors, guess a target
  across areas, or edit/link/move/merge a note.
- `examples/scripts/test_worklist.py` breaks the contract around thresholds, route grouping,
  stable IDs, truncation, input immutability, and the CLI's write boundary.

### Changed
- Blueprint H3 now records the shipped design and acceptance criterion; README adds the artifact,
  quickstart command, and the distinction between a proposal generator and an auto-fixer.

## [1.10.0] - 2026-08-05

### Added
- **Blueprint §5: "Measure the curve, not the snapshot."** An agent-maintained knowledge base does
  not fail abruptly, it degrades — notes accumulate, new ones compete with old ones for the same
  query, the folder a fact lives in slowly stops matching what the fact is about. Every step looks
  fine, which is precisely why one measurement cannot find it.

  Most projects already own the instruments and are using them on the wrong axis. Ours did: a
  retrieval self-test last run at 74 notes, a graph-health report last run at 144, a cost log
  measuring per scheduled job rather than per unit of store. Three readings, three weeks, three
  scales, and no way to answer the only question that matters — is this getting better or worse as
  it grows?

  The section gives the four properties that separate a curve from a log: **size on the x-axis**, not
  time ("cost rose between 74 and 163 notes" is actionable; "cost rose in August" invites excuses);
  **show the delta**, because absolute numbers sit inside their thresholds for a long time while
  drifting toward them; **record the bad runs**, since a curve written only when the gate is green
  documents nothing; and **missing is not zero** — backfilled or unavailable cells must render as
  unknown, because a fabricated cell poisons every comparison drawn across it.

  Plus two traps: import the existing metric function rather than reimplementing the calculation
  (two tools counting the same store and disagreeing is a failure this project has hit repeatedly,
  and it is worst when the two numbers coincide by accident), and resist gating on the curve the
  moment you have it — the curve is evidence; choosing the threshold that turns evidence into a
  refusal is a judgement that belongs to a person.

  The framing is credited, not ours: Zhou et al., *Filesystem-Based Memory for LLM Agents*
  (arXiv:2607.26637), which measures quality, cost and store health as functions of store size rather
  than at a point, and finds that organised stores roughly halve retrieval cost on large material
  while answer quality barely moves — *"quality benchmarks remain largely blind to shape."* Hence the
  headline column is cost, with quality beside it rather than in front of it.

## [1.9.0] - 2026-08-05

### Added
- **Blueprint §5: "A test that breaks a real document must be able to put it back — or shout."**
  Contract-breaking tests are the ones worth having, and the tempting way to write them — corrupt the
  *real* document, assert the checker goes red, restore in `finally` — makes the test a process that
  deliberately writes wrong data into the source of truth, with one ordinary `try/finally` standing
  between that and a corrupted vault.

  `finally` is not a guarantee. On a synced folder it fails for a boring reason: the file is locked
  by the sync client or an open editor. The restore raises, the run continues, the document stays
  wrong. This happened twice in the source project; the second time the suite lowered a tag count by
  one *inside the rules document itself* and left it there, while every visible signal stayed
  plausible — the same two suites red they had been for days. Nothing reported that the constitution
  had been edited.

  The section gives the two ways out in order: **sandbox** the fixture vault and break the copy —
  which is what this repo's `test_auto_fix.py` already does, and why the repo never had this bug — or,
  when the test must exercise real production data, **guard** it: snapshot to disk outside the vault
  before breaking anything, restore atomically with a retry that outlives a sync pause, then verify
  byte-for-byte at the end and be loud plus non-zero if anything still differs. Plus the cascade trap:
  build each case from the snapshot, never from what is currently on disk, or one failed restore
  silently becomes the "original" for every case that follows.

  Generalised: *a mechanism that writes must be judged by what happens when its cleanup fails, not by
  what happens when it succeeds.* The H2 auto-fixer earns its keep through its refusals; a destructive
  test earns its keep through its restore path. Both are only as good as the path nobody watches.

## [1.8.1] - 2026-08-04

### Fixed
- **One of the tests shipped in 1.8.0 could not tell the two behaviours apart.** The case asserting
  that drift is attributed to the right marker only looked for `DRIFT 'subset_count'` in the output —
  and the *previous* whole-line scan reports that same claim too, just against a number the marker
  does not own. It passed before and after the change, which makes it decoration.

  Caught by running every new case against the old behaviour, which is the check that 1.8.0's own
  notes claimed for it. The expectation now pins the number: `document says 4, source of truth says
  5`. Both cases in the pair now fail on the old checker and pass on the new one.

  The demo vault's counts are read from `--show` rather than written into the test, for the same
  reason the tool exists: a number copied out of its source drifts away from it.

## [1.8.0] - 2026-08-04

### Fixed
- **A marker now owns the text before it, so a number belongs to exactly one claim.** The drift
  checker matched each claim's patterns across the whole line and took the first hit, so a line
  carrying two markers of the same unit handed *both* of them the leftmost number. The consequences
  went in two directions and the second one is the reason this needed fixing at the sensor:

  - the report named the wrong token — it said "document says 16" about a marker whose number,
    plainly visible to a human, was 7;
  - `auto_fix.py` then planned two rewrites at one column and had to refuse the line entirely.
    A rule appeared in the README to protect the tool from itself: *at most one marker per unit per
    line.* Rules that exist because a sensor is ambiguous tend to outlive anyone's memory of why.

  `check_rules_drift.marker_segments()` now cuts the line at the markers and reads each number in
  the segment running from the previous marker up to this one. A line with a single numeric marker
  still reads the whole line, so no existing document changes behaviour and nobody has to move a
  marker to sit right after its number.

- **`auto_fix.owned_window()` keeps the fixer inside the same boundary.** Making only the checker
  position-aware would have opened a quieter hole: when two markers on a line carry numbers that are
  *already equal*, exactly one fix is planned, nothing overlaps, and `drop_ambiguous` — the guard
  built for this family of bugs — never sees it. A fixer re-searching the whole line would then
  rewrite the leftmost match, which is the number that was already correct, and leave the wrong one
  standing. Test `9b` is that case; it fails on the previous fixer.

  `drop_ambiguous` stays as a last-resort invariant (disjoint segments mean two valid fixes can no
  longer overlap) with test `9c` calling it directly, because a guard nothing can trigger is a guard
  nobody notices breaking.

- Tests: two cases in `test_drift_check.py` (both numbers right ⇒ no false alarm; second number
  wrong ⇒ the drift is reported against the number that marker owns) and three in `test_auto_fix.py`
  (`9`, `9b`, `9c`). Each new case was **run against the old behaviour first and observed to fail** —
  a case that passes both before and after proves nothing.

## [1.7.1] - 2026-08-04

### Added
- **Seven diagrams.** The repo explained a control loop entirely in prose, which is a strange thing
  for a document whose central claim is that one edge — the one that comes *back* — separates a
  cockpit from a harness. Mermaid, so the diagrams stay diffable text with no binary assets and no
  build step, consistent with the zero-dependency rule everywhere else here.

  - `README.md`: the closed loop itself, and a map of how the shipped artifacts run on an ordinary
    morning (which script is a sensor, which one is allowed to write, where the guard sits).
  - `blueprint.md` §4: open loop vs. closed loop side by side — the point being that judgment stays
    with the human in *both*; closing the loop only removes the cases where there was no decision
    to make.
  - §5: the single source of truth, the marked restatements, the drift checker, and the fix path
    that feeds back into it.
  - §6 H2: the auto-fixer as a flowchart, drawn so that every refusal is visible — held file,
    failed snapshot, disagreeing sources, red gate — because the refusals are the design.
  - §6 H4: a sequence diagram of two streams racing for one file and the hook returning exit 2.
  - §6 H4b: the incident as a timeline (who read what, at what second, while the audit was still
    writing), plus the repaired chain showing that each of the three timeouts has a *different*
    correct answer.

  Labels avoid `<b>`/`<i>`; renderers that sanitise HTML in node labels would otherwise print the
  tags as literal text. `<br/>` is handled in every mode and is the only markup used.

## [1.7.0] - 2026-08-04

### Added
- **`routine_guard.py` — the order between scheduled routines, as a mechanism (H4b).** H4 keeps two
  writers off one file; this is the other half of the same problem. Daily routines have a real
  dependency order (the audit writes a report, the fixer consumes it, the log routine reads the
  session transcripts of both), and the only thing enforcing it was the gap between cron times.
  That is an assumption about the environment, not a guardrail.

  Ours slipped: the agent app sat closed past every scheduled slot and was reopened mid-morning, so
  the scheduler fired **all five overdue routines within two minutes**, in an order unrelated to
  their cron times. The fixer read the audit report four minutes *before* the audit wrote it, froze
  a four-day-old snapshot into the handling log and filed a work item on a false premise; the
  performance log started one second after the audit, scanned a transcript still being written, and
  recorded "9s / 149K tokens" for a run that actually took 6m08s / 6.1M — a number that then
  travelled as evidence. Every routine reported success.

  `wait-report` blocks until the audit report carries today's date; `wait-quiet` blocks until no
  other scheduled run has written to its transcript for `--idle` seconds; `status` prints both.
  Waiting on **data** rather than on a lock is deliberate: a lock needs the upstream routine to
  cooperate with begin/end, and a routine that dies mid-run leaves an orphan lock you then need
  another mechanism to expire. It also means only downstream ever waits — the chain is acyclic, so
  deadlock is impossible by construction.

  Fail-open and fail-closed point in opposite directions on purpose. Unreadable sessions directory
  → proceed (jailing the logger is worse than one row that heals on the next run). Unreadable
  report → stand down (proceeding without knowing whether the upstream stage ran is exactly how the
  wrong conclusion got written).
- `test_routine_guard.py`: 13 break-the-wait cases against a throwaway report and a fake sessions
  directory — stale report must time out rather than pass, a report that appears mid-wait must be
  picked up (and must really have waited), missing report fails closed, a report dated tomorrow
  fails closed, a human's interactive session is never mistaken for a routine, and the logger never
  waits on itself. One case pins an invariant instead of a comment: the guard's idle threshold must
  stay above the in-flight threshold of whatever reads those transcripts, or the guard says "all
  quiet, go" while the reader still skips that session and the row vanishes for a day.

### Changed
- `blueprint.md` §6: new **H4b** section — the incident, why spreading cron times further apart is
  not the fix, why scheduler jitter had already been truncating one row every single morning
  (fixer at ~08:30, logger at ~08:31, fixer runs 3–4 minutes), and the generalisable lesson: **a
  truncated measurement is more dangerous than a missing one** — a missing row announces itself,
  while a row carrying a session id, a token count and a timestamp looks exactly like a fact.
- `kb-autofix-daily.SKILL.md`: a step 0 that waits for today's report, and standing down when it
  never arrives. The previous instruction — log the entry anyway, noting the report is stale — is
  what turned one skipped beat into a confident wrong diagnosis, so it is gone.
- `kb-audit-daily.SKILL.md`: states that it waits for nobody, and why adding a wait there would
  create the one cycle this design does not have.

## [1.6.3] - 2026-07-31

### Fixed
- **Two tools that are supposed to agree counted the same vault differently.**
  `generate_catalog.py` has always excluded `attachments/` — a note's supporting files are not
  themselves notes — but `check_rules_drift.py` only excluded what the rules file named, and the
  example rules did not name it. Point both at one vault and they disagreed on the note count by
  exactly the number of stray `.md` files sitting in `attachments/`. In the private vault this
  mirrors, that was one scratch file whose own first line said it was not a note, and it took a
  routine run reporting two different totals to notice.

  The disagreement is worse than either answer being wrong. A count that two tools derive
  independently is the kind of fact people stop checking, so the drift checker was quietly
  measuring a different vault than the catalog while both printed something confident.

  `attachments` is now in the checker's built-in default (so a rules file that omits `scan` is
  safe) *and* in `rules.example.json` (so a rules file copied from the example is safe). An
  explicit `scan.exclude_dirs` still wins — configuration you wrote is not overridden.

### Added
- `test_drift_check.py`: a **pair** of cases for the above. One drops a scratch `.md` under
  `attachments/` and demands silence; the other drops the same file one directory up and demands
  the checker object. The second exists because the first alone cannot tell "correctly ignored"
  apart from "never looked" — a passing test that would pass just as happily against a checker
  that does nothing is not evidence.

## [1.6.2] - 2026-07-28

### Added
- `test_auto_fix.py` case 10: **every companion tool is invoked in a shape that tool accepts.**
  The bug behind it, found in the private implementation this repo mirrors: a companion built on
  argparse *subparsers* wants its global flags before the subcommand, and putting them after
  returns exit 2. A caller that reads "non-zero" as "gate red" then refuses to do any work, every
  run, while printing a sentence that sounds like caution. Nine existing cases missed it because
  every one of them disabled that gate for speed - **a path the tests always switch off is a path
  nobody has ever run.** The case treats exit 0 and 1 as fine and exit 2 (or "unrecognized
  arguments") as the failure, so it catches the whole class rather than one flag order.

## [1.6.1] - 2026-07-28

An independent review of 1.6.0, run the same day, found three defects in the fixer. All three
share a shape worth naming: each one only shows up on input the happy path never produces, which
is exactly what a break-the-contract suite is for. Each fix ships with the case that would have
caught it.

### Fixed
- **CRLF files were silently rewritten to LF.** The fixer read notes with Python's default text
  mode, which normalises line endings on the way in; writing back turned a one-token fix into a
  whole-file rewrite. On a synced vault every other machine sees "the entire document changed".
  Reads are now raw (`newline=""`). New case 8 asserts a CRLF file is still CRLF, comparing bytes —
  a test that compares decoded strings cannot see this bug at all.
- **The lock was consulted, not held.** The fixer asked `claim.py` whether a file was free and then
  wrote — an answer that is stale the moment it is given, since shell writes never pass through the
  hook. It now `take`s every target, releases in a `finally`, and if any file cannot be taken it
  releases what it holds and defers the whole run. It also uses its own stream id instead of
  borrowing the caller's, because ending a run with `release --all` on a session's stream would
  drop claims that session holds for unrelated work.
- **Two claims pointing at the same number corrupted the line.** When one line carries two markers
  of the same unit, the checker hands both claims the leftmost number, so the fixer planned two
  different values for one token; re-locating after each write then landed on the number it had
  just written. Fixes are now applied by recorded column, right to left, with the token verified at
  that offset — and a line where two fixes overlap is **refused entirely** with a reason, rather
  than half-applied and rolled back (which would also discard good fixes in other files). New case
  9 covers it.

### Changed
- README and blueprint state the sensor-level limit plainly: one marker per unit per line. The
  ambiguity starts in the drift checker's report, not in the fixer, and the honest fix there is to
  attribute numbers by marker position — noted as follow-up work rather than papered over.

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

[1.15.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.15.0
[1.14.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.14.1
[1.14.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.14.0
[1.13.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.13.1
[1.13.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.13.0
[1.12.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.12.1
[1.12.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.12.0
[1.11.2]: https://github.com/chuong1224/harness-kb/releases/tag/v1.11.2
[1.11.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.11.1
[1.11.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.11.0
[1.10.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.10.0
[1.9.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.9.0
[1.8.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.8.1
[1.8.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.8.0
[1.7.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.7.1
[1.7.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.7.0
[1.6.3]: https://github.com/chuong1224/harness-kb/releases/tag/v1.6.3
[1.6.2]: https://github.com/chuong1224/harness-kb/releases/tag/v1.6.2
[1.6.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.6.1
[1.6.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.6.0
[1.5.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.5.0
[1.4.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.4.0
[1.3.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.3.0
[1.2.1]: https://github.com/chuong1224/harness-kb/releases/tag/v1.2.1
[1.2.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.2.0
[1.1.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.1.0
[1.0.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.0.0
