---
gate_ignore: true
---

# Agent entrypoint for this knowledge base

Read this file before searching, editing, importing, or running maintenance in this vault.
It is the human-owned source of truth for agent behavior. `CLAUDE.md` is only a pointer here;
do not copy this contract into another agent-specific file.

## At the start of a session

1. Run `python .harness/harness.py check .`. It uses the consent stored in
   `.harness/manifest.json`, caches successful checks, and stays quiet when offline.
2. Read `.harness/manifest.json` before changing harness-owned tooling. It records the exact
   upstream version and commit, ownership, and base hash of every installed component.
3. Search the narrowest relevant index or catalog before broad filesystem search.

## Sources of truth

- Put human policy and domain-specific instructions in this `AGENTS.md`.
- Put enumerable rules (areas, controlled tags, required frontmatter) in
  `.harness/rules/rules.json`; documents may explain those rules but must not become a second
  owner of the same values.
- Files marked `user` in the manifest are yours. Upgrades never overwrite or recreate them.
- Files marked `upstream` are installed tooling. Do not hand-edit them: a local change becomes
  an explicit upgrade conflict instead of being silently overwritten.

## Before and after a change

1. Read the primary source, not only a summary derived from it.
2. Keep private data, credentials, machine-specific paths, and large binaries out of shared
   tooling and public repositories.
3. Make the smallest reversible change that closes the task.
4. Run `python .harness/harness.py verify .` and read each exit code directly. Never pipe a
   gate through `tail`, `grep`, or another command when deciding whether it passed.
5. If a gate is red, fix the cause or restore the change. Do not report completion from a
   partial or degraded run.

## Updating Harness KB

An update notice is evidence, not permission to overwrite files. Pull or clone the newer source,
then use two separate steps:

```text
python .harness/harness.py plan . --source <newer-harness-kb-checkout>
python .harness/harness.py apply . --plan <reviewed-plan.json>
```

The plan shows release distance, migration impact, ownership decisions, and conflicts before any
installed component changes. Apply creates a backup, runs the new gates, and automatically rolls
back a red result. It prints a persistent rollback handle for a later manual rollback.

## Customize this file

Replace this section with the vault's areas, naming conventions, source hierarchy, privacy rules,
and any mandatory domain checks. Keep the lifecycle and verification contract above unless the
replacement provides an equally explicit, tested mechanism.
