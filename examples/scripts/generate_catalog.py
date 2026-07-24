#!/usr/bin/env python3
"""
generate_catalog.py - Build a triage catalog for a Harness KB.

The catalog is a single machine-readable file an agent reads FIRST, before searching
the whole vault. It lets the agent decide "which one note do I open?" from metadata
(title, aliases, summary, tags, area, headings) instead of walking every file and
risking missed data. See docs/blueprint.md - a fresh catalog is a closed retrieval loop.

Regenerate it after every edit (and on a schedule) so it never goes stale.

Zero dependencies: Python 3.8+, standard library only.

Usage:
    python generate_catalog.py /path/to/vault --out catalog.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Reuse the same tiny frontmatter parser as verify_kb.py (kept inline for a standalone file).
EXCLUDE_DIRS = {".git", ".obsidian", "node_modules", "attachments"}
EXCLUDE_PREFIXES = (".", "_")


def strip_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_inline_list(v):
    inner = v.strip().lstrip("[").rstrip("]")
    return [strip_scalar(x) for x in inner.split(",") if x.strip()]


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block, body = text[3:end], text[end + 4:]
    meta, key = {}, None
    for line in block.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s+-\s", line) and key is not None:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(strip_scalar(line.strip()[1:]))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            meta[key] = []
        elif val.startswith("["):
            meta[key] = parse_inline_list(val)
        else:
            meta[key] = strip_scalar(val)
    return meta, body


def headings(body):
    return [re.sub(r"^#+\s+", "", ln).strip() for ln in body.splitlines() if re.match(r"^#{1,6}\s", ln)]


def is_excluded(rel_parts):
    for part in rel_parts[:-1]:
        if part in EXCLUDE_DIRS or part.startswith(EXCLUDE_PREFIXES):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Build a triage catalog for a Harness KB.")
    ap.add_argument("vault", help="path to the vault (folder of Markdown notes)")
    ap.add_argument("--out", default="catalog.json", help="output JSON path (default: catalog.json)")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"error: not a directory: {vault}", file=sys.stderr)
        return 2

    entries, n_index = [], 0
    for p in sorted(vault.rglob("*.md")):
        rel = p.relative_to(vault)
        if is_excluded(rel.parts):
            continue
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        aliases = meta.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        is_index = ("index" in tags) or str(meta.get("type", "")).lower() == "index" or p.stem.lower().startswith("index")
        if is_index:
            n_index += 1
        entries.append({
            "id": p.stem,
            "title": meta.get("title", p.stem),
            "path": str(rel).replace("\\", "/"),
            "area": rel.parts[0] if len(rel.parts) > 1 else "(root)",
            "type": meta.get("type", ""),
            "tags": tags,
            "aliases": aliases,
            "summary": meta.get("summary", ""),
            "headings": headings(body),
            "is_index": is_index,
            "has_attachments": (p.parent / "attachments").is_dir(),
        })

    tags_seen = sorted({t for e in entries for t in e["tags"]})
    catalog = {
        "generated_from": str(vault),
        "count": len(entries),
        "indexes": n_index,
        "tags": tags_seen,
        "notes": entries,
    }
    Path(args.out).write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"notes: {len(entries)} (indexes: {n_index})")
    print(f"tags:  {', '.join(tags_seen) or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
