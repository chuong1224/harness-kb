# The Self-Operating Knowledge Base — A Harness Blueprint

> This blueprint distills a working architecture for a knowledge base (KB) that maintains
> *itself*: correct, fresh, and fast for both humans and AI agents to retrieve from. It is
> deliberately tool-agnostic — the KB is just a folder of Markdown notes; the ideas apply
> whether you drive it with an editor, a script, or an autonomous agent.

*Provenance: this blueprint reflects a real working system as of **2026-07-25**. The concepts are
evergreen; any specific counts mentioned (e.g. tag or index totals) are illustrative snapshots
from that date, not live values.*

---

## 1. The goal: a KB that runs itself

**North star:** the KB preserves its own *correctness*, *freshness*, and *retrieval quality*
without a human steering every loop. The human becomes a *supervisor and approver of high-risk
changes*, not the *operator* of routine maintenance.

This is not automation for its own sake. It follows directly from the real goal of a KB an agent
depends on: **the agent must not miss data**. A KB whose rules contradict themselves, whose
indexes are stale, and whose links quietly break will lead an agent to triage wrongly and miss
what it needed. To guarantee "no missed data" as the KB grows, the quality-control loops must
**run and correct themselves** — they cannot depend on a human remembering to run them.

---

## 2. What "harness" really means

There are two senses of the word, and a maturing system sits at two different levels:

- **Sense 1 — agent scaffolding.** The runtime around a model: tools, permissions, hooks,
  feedback. Building a *second-order* harness on top of your agent runtime — activity logging,
  verification gates, coordination contracts — already puts you ahead of typical usage.
- **Sense 2 — the original sense (a harness on a powerful animal, a test harness, a safety
  harness).** The thing that makes a **powerful but unpredictable** engine **safe and reliably
  useful.** This is the level most systems never fully reach.

> **The defining property of a harness is not "many gauges." It is the CLOSED LOOP:**
> `sense deviation → decide → self-correct → verify → keep on rails`, running with minimal
> human intervention. A hundred dashboards where a human still fixes everything by hand is a
> **cockpit**, not a harness. A harness is when the system **pulls itself back onto the rails.**

---

## 3. The 7-property scorecard

Score any KB honestly. The first six properties are the *scaffolding*; the seventh is the
*essence*. Most effort — and most pride — goes into 1–6, which is exactly why 7 is the frontier.

| Property | What good looks like | Typical state |
|---|---|---|
| **Actuation** | Agent can read/write notes and run maintenance | Usually solved |
| **Observability** | Every action is logged and visible in real time | Often strong |
| **Guardrails** | Damage is prevented before it happens (gates, backups, safe entry points) | Often strong |
| **Determinism / self-healing** | Entry points are idempotent; a supervisor recovers from crashes | Achievable |
| **Automation** | Routine maintenance runs on a schedule without a human | Achievable |
| **Multi-agent coordination** | Multiple agents share the KB without clobbering each other | Usually by convention |
| **Closed control loop** | The system corrects itself; it does not only report | **Rarely reached** |

A common, honest self-assessment in a maturing system: near-complete on scaffolding and
guardrails, but only a fraction of the top-level loops actually close. You have built an excellent
*cockpit and safety rig*; what remains is letting the machine **hold itself on the rails**.

---

## 4. Diagnosing your loops: open vs. closed

For each maintenance concern, ask one question: **does it sense AND self-correct, or does it only
sense-and-report?** A loop that reports and stops is an *open* loop — a human is the final
actuator.

| Loop | Sensor | Self-corrects? | State |
|---|---|---|---|
| **Code/config changes** | test/self-check | Change → gate must pass → back up | Closed |
| **Retrieval catalog freshness** | note changes | Regenerate on schedule + on every edit | Closed |
| **Content integrity (audit)** | daily audit finds violations | Often intentionally read-only | **Half-open** |
| **Retrieval optimization** | metrics: re-reads, long chains, hot/cold notes | Nothing restructures automatically | **Open** |
| **Cost / performance** | per-run token/cost/time logs | Observe only | **Open** |

If most of your top-level loops sense-and-report, you have a cockpit. That is the line between
"a very rich instrumentation rig" and "a harness in the full sense."

---

## 5. The core architecture debt: a single source of truth

A recurring failure mode: **rules duplicated across many documents drift apart.** An audit that
checks the KB against its own rules will eventually catch its *rules* disagreeing with each other
— for example, one document might claim the tag vocabulary has 14 entries while another says 15,
or one counts 26 indexes and another 27. The audit catching the drift is good. But the drift **keeps recurring** because there
is no single generated source.

> **A real harness has exactly ONE source of truth.** Every countable, enumerable fact (the tag
> vocabulary, the number of indexes, the list of areas) is **generated from one source**;
> every other document *derives* from it — instead of five hand-copied versions slowly diverging
> and an audit chasing them every morning.

This is the highest-leverage, lowest-risk fix, and it is a prerequisite for safely automating any
*correction* (§6, §7). See `examples/rules/rules.example.json` for the data-as-source pattern and
`examples/scripts/verify_kb.py` for a gate that checks notes against that single source.

### Documents may show the numbers - they just may not own them

The naive reading of "one source of truth" is to strip every count out of your prose. Do not: readers
and agents genuinely want "the vocabulary has 7 tags" in the document they are already reading. The
fix is not to delete the copy, it is to **stop letting the copy be authoritative**.

Mark each restating line with a comment naming the claim it makes:

```markdown
## Tag vocabulary (7 tags) <!-- rules:tag_count -->
## Index hierarchy (12 indexes) <!-- rules:index_count -->
```

The marker is an HTML comment: invisible when rendered, trivial to grep, and it survives editing by
humans and agents alike. A checker then reads each registered document, extracts the marked claims,
and compares them with the source - **policy** values as authored (vocabulary, areas) and
**filesystem** values as counted (index count). Drift is reported as `file:line`, so nobody has to
hunt for which of five copies is wrong. `examples/scripts/check_rules_drift.py` is a working
implementation; `examples/scripts/test_drift_check.py` breaks it on purpose to prove it catches drift.

Two properties matter more than the implementation:

- **The registry is explicit.** The rules file lists which documents restate which claims, so
  *deleting* a marker is itself an error. Drift cannot hide by removing the evidence.
- **Detection is separate from correction.** The checker never edits a document. It only makes the
  numbers impossible to break silently - which is exactly the precondition H2 needs before anything
  is allowed to rewrite them (§7, rule 4).

### The tooling has to live in the vault, not beside it

One source of truth applies to the *tools* as much as to the data. The tempting place to put a
generator or a checker is wherever your agent keeps its skills, or a config directory, or the folder
you happened to clone it into. All of those live **outside** the vault - and that single decision
quietly breaks three things at once:

- **The KB stops being self-sufficient.** A machine that has the vault but not the tool cannot
  regenerate the catalog or run the gate. Work done there silently degrades the KB: notes get added,
  derived artifacts go stale, and nobody notices until someone sits at the *other* machine.
- **The tool itself becomes a drift source.** Two machines, two copies, edited on different days.
  Same vault, two answers - and no way to tell which one is right.
- **It lands in the audit's blind spot.** The audit scans the vault. A checker outside the vault is,
  by construction, unchecked. We found a path constant that had been broken for a week inside exactly
  that gap: every session using it silently fell back to a slower, lossier retrieval route.

> **Rule: anything the KB needs in order to verify or regenerate itself belongs inside the KB**,
> versioned with the content it acts on, depending on nothing but a stock interpreter.

What may stay outside is narrow and specific: **secrets**, **large binaries**, and **per-machine
configuration** (paths, ports). A thin launcher or hook may live in machine config as long as it only
*calls into* the in-vault script and holds no logic of its own.

The scaling argument is the one that matters. With two machines you can patch by hand and remember
which is behind. At team or company scale, per-machine copies grow linearly while the chance that
they agree collapses: N machines is N chances for the gate to be missing, outdated, or subtly
different — and every one of those is invisible to an audit that only reads the vault. In-vault
tooling makes that class of failure structurally impossible instead of merely unlikely.

The test is blunt: **clone the vault onto a clean machine. Can you run the gate?** If the answer
requires installing something first, the tooling is in the wrong place.

One migration caveat, learned the hard way. When you move a generator into the vault, its output must
match the old one **field for field** — otherwise the two machines take turns rewriting the same
derived file and every sync becomes a diff. Port it, then run it in a compare mode against the
artifact the old tool produced and reconcile until only genuine content changes remain. Undocumented
conventions surface fast that way: slug truncation limits, whether headings include the H1, what a
missing frontmatter field defaults to.

---

## 6. Roadmap: closing the loops

Not "add more gauges" — **close the loop.** Ordered by return-on-investment vs. risk.

### H1 — One source of truth for enumerable rules  ⭐ *(do this first; reference implementation included)*
Put every countable fact (tag vocabulary, area list, index count) in **one data file**. Make every
document *derive* from it, and have the audit compare notes against that source directly rather
than comparing hand-copied lists against each other. This kills the entire class of "14 vs 15"
drift at the root.

**Shipped here:** `examples/rules/rules.example.json` (the source, with a registry of which documents
restate which claims) + `examples/scripts/check_rules_drift.py` (the checker, exit 1 on drift) +
`examples/scripts/test_drift_check.py` (break-the-gate tests). See §5 for the marker pattern.
Wire the checker into the daily audit and the class of drift stops being something a human notices.

### H2 — Promote the audit from "report" to "safe auto-fix"
For **mechanical, reversible** errors (broken links, missing frontmatter fields, count mismatches,
orphan images), let the audit **fix + back up + log + keep a rollback path.** Keep everything that
requires *judgment* (translation, semantic tag classification, note structure) strictly read-only.

### H3 — Turn retrieval signals into actions
Metrics (frequently re-read notes, long retrieval chains, isolated hot notes) currently just sit on
a dashboard. Close the loop by **generating suggestions** — "link notes A and B", "merge two notes
on the same topic", "note X is an orphan, add it to an index" — into an actionable worklist, instead
of waiting for a human to read the dashboard and infer the fix.

### H4 — Mechanical coordination instead of convention
If multiple agents/sessions share the KB, replace "please check the version before editing" with a
**mechanical lock** (a lock file / per-file claim). This turns a social contract into a hard
guardrail and ends the class of bugs where two parallel streams clobber each other.

*(Optional, later — H5: feed the cost/performance logs into a threshold that warns or suggests a
cheaper model when a scheduled run exceeds its token budget.)*

---

## 7. Safety: how far to automate the "fix"

> **A read-only audit is the RIGHT instinct, not unfinished work.** You must never let an agent
> silently rewrite the KB's constitution at 8 a.m. The remaining distance to a full harness is not
> "work left undone" — it is a design question: *how far do you dare automate the correction, and
> with what guardrails?* The real meaning of "harness" lives precisely here.

Safety rules for closing any correction loop (applies to H2/H3):

1. **Only auto-fix MECHANICAL + REVERSIBLE errors.** Anything requiring semantic judgment stays
   read-only and goes to a worklist for a supervised session.
2. **Every auto-fix must: back up first, log the change, and keep a rollback path.** Never fix
   silently.
3. **Idempotent + gated.** After an auto-fix, the integrity gate must report zero problems for the
   fix to count as a closed loop; on failure, roll back and alert a human.
4. **A single source of truth (H1) is a prerequisite** for auto-fixing counts — without it, an
   auto-fix can "correct" toward a wrong copy.

---

## 8. Acceptance criteria — "harness, not cockpit"

The KB qualifies as a harness in the full sense when these are **measurable**:

- [ ] **Zero drift:** enumerable facts no longer disagree between documents — the audit reports
      zero "count/enumeration" conflicts for 30 consecutive days (via H1).
- [ ] **At least one class of mechanical error is auto-fixed** by the audit (no manual session),
      with a log and a rollback proving it is safe (via H2).
- [ ] **Retrieval signals become an actionable worklist**, not just a dashboard (via H3).
- [ ] **Multi-agent access uses a mechanical lock**, not discipline (via H4).
- [ ] **Every operational loop has all five stages:** sensor + threshold + action + verification +
      rollback.

When all five are green, the human is genuinely reduced to *supervising and approving high-risk
changes* — the definition in §1.

---

## Appendix: the loop, as a checklist

Any maintenance concern you want to "harness" should be able to answer all five:

1. **Sensor** — what detects the deviation, and how often?
2. **Threshold** — what counts as "out of bounds"?
3. **Action** — what correction fires automatically?
4. **Verification** — how do you confirm the correction worked (a gate that can fail)?
5. **Rollback** — how do you undo it safely if verification fails?

If you can only answer 1 and 2, you have a gauge. All five is a harness.
