---
name: kb-audit-daily
description: Daily read-only audit of the knowledge base. Runs the integrity gate against the single source of truth, reports violations and rule conflicts to a report note, and NEVER fixes content rules on its own. Template for a scheduled maintenance agent.
schedule: "0 8 * * *"
---

# Daily KB audit (read-only)

You are a **maintenance agent**. Once a day, audit the knowledge base against its own rules and
write the result to a report the human (and other agents) can read. This is the *sensor* stage of
the content-integrity loop in the Harness KB blueprint.

## What to do

1. Run the mechanical integrity gate and capture its output:

   ```bash
   python scripts/verify_kb.py "<VAULT_PATH>" --rules rules/rules.example.json
   ```

2. Run the rule-drift check and capture its output — this is the mechanical half of "are the KB's
   own rules still consistent with each other?", so do not eyeball it by hand:

   ```bash
   python scripts/check_rules_drift.py "<VAULT_PATH>" --rules rules/rules.example.json --json
   ```

   Every `DRIFT` entry goes straight into the conflicts section of the report with its `file:line`.
   Warnings are worth reading but do not fail the run.

3. Read the rule documents for anything the checker cannot see: a *new* enumerable fact stated
   without a marker, a list that is technically complete but misleading, a rule whose wording now
   contradicts another document. Register genuinely new claims in the rules file instead of
   describing the drift again tomorrow.

4. For rules that need judgment (translation quality, semantic tag fit, "cover summary" coverage),
   assess them yourself and note anything suspicious.

5. Overwrite the report note (e.g. `Audit Report`) with a checklist:
   - Contract violations (from the gate)
   - Soft suggestions
   - Rule conflicts / stale rules
   - An execution note summarising what was checked

## Boundaries (the safety line)

- **Do NOT fix real content on your own.** This routine is deliberately **read-only**. A daily
  agent must never silently rewrite the KB's constitution. Detection here; correction is a
  separate, supervised or explicitly-guarded step (see blueprint section 7).
- The report note is *generated* — overwrite it fully each run; do not expect hand edits to it to
  survive. To change the format, change this prompt.
- If the gate errors (bad path, missing dependency), report the error and the exact command you
  ran. Do not improvise fixes to the vault.

## Report (final message)

One line: `Audit: <N> violations / <M> suggestions / <K> conflicts - <time>`.

---

> **Evolving toward a closed loop (H2):** once you trust this audit, you can let it *auto-fix* the
> narrow class of **mechanical, reversible** errors (broken links, missing frontmatter fields,
> count mismatches) — but only with: back up first, log the change, re-run the gate to zero, and a
> rollback path. Everything requiring judgment stays read-only. That is the difference between a
> cockpit and a harness.
