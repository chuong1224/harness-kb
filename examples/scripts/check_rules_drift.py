#!/usr/bin/env python3
"""
check_rules_drift.py - Keep documents honest against the single source of truth (H1).

The failure mode this closes: enumerable rules (tag vocabulary, area list, index count)
get hand-copied into several documents, then drift apart - one doc says 14 tags, another
says 15. An audit catches it, a human fixes the copies, and a week later it happens again,
because nothing generates those numbers from one place.

This script implements the "derive, do not copy" half of H1 (docs/blueprint.md section 5):

  1. The rules file is the ONE source. Policy values (vocabulary, areas) are authored there;
     filesystem values (index count) are counted at run time.
  2. Documents may still show the numbers - readers want them - but each such line carries a
     marker comment naming the claim it makes:

         ## Tag vocabulary (7 tags) <!-- rules:tag_count -->

     Markers are HTML comments: invisible when the Markdown is rendered, trivial to grep.
  3. This script reads every registered document, extracts the marked claims, and compares
     them with the source. Disagreement is an error with an exact file:line, so the fix is
     never a hunt.

It deliberately does NOT edit anything. Auto-correcting the numbers is H2 territory and needs
a backup + log + rollback path first; the markers make that safe later, because they say exactly
which token may be rewritten.

Companion gate: verify_kb.py validates NOTES against the rules (frontmatter, tags, links).
This script validates DOCUMENTS' claims ABOUT the rules. Different loops, same source.

Exit code 0 = no drift (warnings allowed), 1 = drift found, 2 = usage error.
Zero dependencies: Python 3.8+, standard library only.

Usage:
    python check_rules_drift.py /path/to/vault --rules rules.example.json
    python check_rules_drift.py /path/to/vault --rules rules.example.json --json
    python check_rules_drift.py /path/to/vault --rules rules.example.json --show
"""
import argparse
import json
import re
import sys
from pathlib import Path

UNIT_PATTERNS = {
    "tag": re.compile(r"(\d+)\s*tags?\b", re.IGNORECASE),
    "area": re.compile(r"(\d+)\s*areas?\b", re.IGNORECASE),
    "index": re.compile(r"(\d+)\s*index(?:es|s)?\b", re.IGNORECASE),
}


def strip_markup(line, marker_re):
    """Drop marker comments and light Markdown emphasis so the numbers read plainly."""
    return marker_re.sub(" ", line).replace("**", "").replace("`", "")


def marker_segments(line, marker_re, claim_types):
    """[(claim, the text that marker OWNS, was it cut)] in the order the markers appear.

    A marker owns the text between the previous marker and itself, because a marker is
    written just after the number it guards.

    With ONE numeric claim on the line the segment is the WHOLE line, exactly as before.
    Nothing forces an author to put the marker after the number when there is nothing to
    confuse it with.

    Two or more numeric claims is where the whole-line scan used to break: it took the
    FIRST pattern match and handed it to every claim, so the second marker reported a
    number belonging to the first. That is wrong twice over - the report points at the
    wrong token, and an auto-fixer working from that report aims two rewrites at one
    column and has to refuse the line. Cutting per marker removes the ambiguity at the
    sensor instead of teaching every consumer to work around it.
    """
    occurrences = [(m.group(1), m.start(), m.end()) for m in marker_re.finditer(line)]
    numeric = [c for c, _s, _e in occurrences
               if (claim_types.get(c) or {}).get("kind") != "list"]
    if len(numeric) <= 1:
        whole = strip_markup(line, marker_re)
        return [(claim, whole, False) for claim, _s, _e in occurrences]
    # Blank the markers with SAME-LENGTH spaces: strip_markup deletes them, which shifts
    # every offset, and cutting a segment needs offsets that still match the raw line.
    blanked = marker_re.sub(lambda m: " " * len(m.group(0)), line)
    segments, previous_end = [], 0
    for claim, start, end in occurrences:
        segments.append((claim, blanked[previous_end:start].replace("**", "").replace("`", ""), True))
        previous_end = end
    return segments


def is_excluded(path, vault, scan):
    prefixes = tuple(scan.get("exclude_dir_prefixes", [".", "_"]))
    # `attachments/` holds a note's supporting files, so a stray .md in there is a
    # scratch file, not a note. It is in the default because generate_catalog.py
    # already excludes it unconditionally: leave it out here and the two tools that
    # are meant to agree report different note counts off the same vault.
    names = set(scan.get("exclude_dirs", [".git", ".obsidian", "node_modules", "attachments"]))
    for part in path.relative_to(vault).parts[:-1]:
        if part in names or part.startswith(prefixes):
            return True
    return False


def frontmatter_end(lines):
    """Line index where the body starts, so historical notes in frontmatter are skipped."""
    if lines and lines[0].startswith("---"):
        for i in range(1, len(lines)):
            if lines[i].startswith("---"):
                return i + 1
    return 0


def tags_of(text):
    m = re.search(r"^tags:\s*\[(.*?)\]\s*$", text, re.MULTILINE)
    if not m:
        return []
    return [t.strip().strip("\"'") for t in m.group(1).split(",") if t.strip()]


def canonical_facts(vault, rules):
    """The single source of truth: policy values as authored + filesystem values as counted."""
    scan = rules.get("scan", {})
    vocab = list(rules["tags"]["controlled_vocabulary"])
    areas = [a["path"] if isinstance(a, dict) else str(a) for a in rules.get("areas", [])]

    notes, indexes, used = [], [], set()
    for p in vault.rglob("*.md"):
        if not p.is_file() or is_excluded(p, vault, scan):
            continue
        notes.append(p)
        if p.stem.lower().startswith("index"):
            indexes.append(p)
        used.update(tags_of(p.read_text(encoding="utf-8", errors="replace")))

    on_disk = sorted(
        d.name for d in vault.iterdir()
        if d.is_dir() and not d.name.startswith(tuple(scan.get("exclude_dir_prefixes", [".", "_"])))
        and d.name not in set(scan.get("exclude_dirs", []))
    )
    return {
        "tag_count": len(vocab),
        "tag_vocabulary": vocab,
        "area_count": len(areas),
        "areas_declared": areas,
        "areas_on_disk": on_disk,
        "index_count": len(indexes),
        "note_count": len(notes),
        "tags_in_use": sorted(used),
        "unused_vocabulary_tags": sorted(t for t in vocab if t not in used),
    }


def unit_hits(line, facts, heuristics, marker_re):
    """Yield (value, unit, agrees) for every 'N <unit>' on a line, skipping prose-sized numbers."""
    minimums = heuristics.get("min_value", {})
    expected = {"tag": facts["tag_count"], "area": facts["area_count"], "index": facts["index_count"]}
    clean = strip_markup(line, marker_re)
    for unit, pattern in UNIT_PATTERNS.items():
        for m in pattern.finditer(clean):
            value = int(m.group(1))
            if value < minimums.get(unit, 0):
                continue  # e.g. "index files carry 1 tag only" is prose, not a vocabulary count
            yield value, unit, value == expected[unit]


def check_document(vault, rules, facts, consumer, marker_re):
    errors, warnings = [], []
    docs = rules.get("documents", {})
    claim_types = docs.get("claim_types", {})
    heuristics = docs.get("scan_heuristics", {})
    rel = consumer["path"]
    path = vault / rel
    if not path.is_file():
        return [f"{rel}: registered as a consumer but the file is missing"], []

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    body_start = frontmatter_end(lines)
    ignore = [re.compile(p) for p in consumer.get("ignore_lines", [])]
    retired_ok = [re.compile(p, re.IGNORECASE) for p in heuristics.get("retired_mention_ok", [])]

    seen, marked_lines = set(), set()
    for i, line in enumerate(lines):
        segments = marker_segments(line, marker_re, claim_types)
        if not segments:
            continue
        marked_lines.add(i)
        clean = strip_markup(line, marker_re)
        for claim, segment, owned in segments:
            seen.add(claim)
            spec = claim_types.get(claim)
            if spec is None:
                errors.append(f"{rel}:{i+1}: marker '{claim}' is not defined in documents.claim_types")
                continue
            if spec.get("kind") == "list":
                continue  # whole-file claim, handled below
            expected = facts.get(spec.get("source"))
            found = None
            for pattern in spec.get("patterns", []):
                m = re.search(pattern, segment)
                if m:
                    found = int(m.group(1))
                    break
            if found is None:
                where = "in the text this marker owns" if owned else "on the line"
                errors.append(f"{rel}:{i+1}: marker '{claim}' but no number readable {where} -> {clean.strip()[:90]}")
            elif found != expected:
                errors.append(f"{rel}:{i+1}: DRIFT '{claim}' - document says {found}, source of truth says {expected} -> {clean.strip()[:90]}")

    for claim, spec in claim_types.items():
        if spec.get("kind") == "list" and claim in seen:
            missing = [t for t in facts["tag_vocabulary"] if f"`{t}`" not in text]
            if missing:
                errors.append(f"{rel}: enumeration is incomplete, missing {', '.join(missing)}")

    for claim in consumer.get("expect", []):
        if claim not in seen:
            errors.append(f"{rel}: marker '{claim}' is gone (the rules file says this document must carry it)")

    for i, line in enumerate(lines):
        if i < body_start or any(r.search(line) for r in ignore):
            continue
        if not any(r.search(line) for r in retired_ok):
            for dead in rules.get("documents", {}).get("retired_tags", []):
                if f"`{dead}`" in line:
                    warnings.append(f"{rel}:{i+1}: still mentions retired tag '{dead}'")
        if i in marked_lines:
            continue
        for value, unit, agrees in unit_hits(line, facts, heuristics, marker_re):
            if not agrees:
                warnings.append(f"{rel}:{i+1}: unmarked number disagrees with the source ({value} {unit}) -> {line.strip()[:90]}")
    return errors, warnings


def check_policy(facts):
    """Policy vs filesystem: declared areas are allowed to be aspirational, so these are warnings."""
    warnings = []
    for name in facts["areas_declared"]:
        if name not in facts["areas_on_disk"]:
            warnings.append(f"rules: area '{name}' is declared but has no folder in the vault")
    for name in facts["areas_on_disk"]:
        if name not in facts["areas_declared"]:
            warnings.append(f"rules: folder '{name}' exists but is not declared as an area")
    for tag in facts["unused_vocabulary_tags"]:
        warnings.append(f"rules: vocabulary tag '{tag}' is not used by any note")
    return warnings


def main():
    ap = argparse.ArgumentParser(description="Check documents against the single source of truth.")
    ap.add_argument("vault", help="path to the vault (folder of Markdown notes)")
    ap.add_argument("--rules", required=True, help="path to the rules JSON file (single source of truth)")
    ap.add_argument("--json", action="store_true", help="machine-readable output for a scheduled audit")
    ap.add_argument("--show", action="store_true", help="print the canonical facts and exit")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"error: not a directory: {vault}", file=sys.stderr)
        return 2
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    docs = rules.get("documents", {})
    prefix = docs.get("marker_prefix", "rules")
    marker_re = re.compile(r"<!--\s*" + re.escape(prefix) + r":([a-z_]+)\s*-->")

    facts = canonical_facts(vault, rules)
    if args.show:
        print(json.dumps(facts, ensure_ascii=False, indent=2))
        return 0

    errors, warnings = [], check_policy(facts)
    consumers = docs.get("consumers", [])
    for consumer in consumers:
        e, w = check_document(vault, rules, facts, consumer, marker_re)
        errors += e
        warnings += w

    if args.json:
        print(json.dumps(
            {"ok": not errors, "errors": errors, "warnings": warnings, "canonical": facts},
            ensure_ascii=False, indent=2,
        ))
        return 1 if errors else 0

    print(f"Vault: {vault}")
    print(f"Source of truth: {Path(args.rules).resolve()}")
    print(f"Canonical: {facts['tag_count']} tags | {facts['area_count']} areas | "
          f"{facts['index_count']} indexes | {facts['note_count']} notes")
    print(f"Documents checked: {len(consumers)}")
    print(f"Drift: {len(errors)} | Warnings: {len(warnings)}\n")
    for e in errors:
        print(f"  [DRIFT] {e}")
    for w in warnings:
        print(f"  [WARN ] {w}")
    if not errors:
        print("  no drift - every marked claim matches the source of truth")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
