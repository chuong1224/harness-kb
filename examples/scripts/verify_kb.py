#!/usr/bin/env python3
"""
verify_kb.py - Integrity gate for a Harness KB.

This is the "verify" step of the control loop described in docs/blueprint.md.
It scans a folder of Markdown notes (a "vault") and reports violations of the
rules defined in a single source-of-truth file (see examples/rules/rules.example.json).

Exit code 0 = clean, 1 = problems found. Wire it into a commit hook or a scheduled
audit as a hard gate: no green, no "done".

Zero dependencies: Python 3.8+, standard library only.

Usage:
    python verify_kb.py /path/to/vault --rules rules.example.json
    python verify_kb.py /path/to/vault              # uses built-in default rules
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_RULES = {
    "tags": {"controlled_vocabulary": [], "index_only_tag": "index"},
    "frontmatter": {"required": ["title", "aliases", "summary", "tags"]},
    "attachments": {
        "binary_extensions_requiring_digest": [".xlsx", ".xls", ".csv", ".pdf", ".docx", ".pptx"],
        "image_extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"],
    },
    "scan": {"exclude_dirs": [".git", ".obsidian", "node_modules"], "exclude_dir_prefixes": [".", "_"]},
}

WIKILINK = re.compile(r"(!?)\[\[([^\]]+)\]\]")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def strip_markdown_code(text):
    """Mask fenced and inline code while preserving line breaks and offsets.

    Syntax examples are not knowledge-graph edges. Supporting multi-backtick inline
    delimiters and both CommonMark fence characters prevents the verifier from
    reporting links shown in documentation as real broken links.
    """
    out = []
    fence_char = None
    fence_len = 0

    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        ending = raw[len(body):]

        if fence_char is not None:
            stripped = body.lstrip(" ")
            indent = len(body) - len(stripped)
            run = len(stripped) - len(stripped.lstrip(fence_char))
            closes = indent <= 3 and run >= fence_len \
                and not stripped[run:].strip(" \t")
            out.append(" " * len(body) + ending)
            if closes:
                fence_char = None
                fence_len = 0
            continue

        fm = FENCE_OPEN_RE.match(body)
        if fm:
            fence = fm.group(1)
            info = fm.group(2)
            if fence[0] == "~" or "`" not in info:
                fence_char = fence[0]
                fence_len = len(fence)
                out.append(" " * len(body) + ending)
                continue

        chars = list(body)
        i = 0
        while i < len(body):
            if body[i] != "`":
                i += 1
                continue
            run_end = i + 1
            while run_end < len(body) and body[run_end] == "`":
                run_end += 1
            width = run_end - i
            j = run_end
            close_end = None
            while j < len(body):
                j = body.find("`", j)
                if j < 0:
                    break
                end = j + 1
                while end < len(body) and body[end] == "`":
                    end += 1
                if end - j == width:
                    close_end = end
                    break
                j = end
            if close_end is None:
                i = run_end
                continue
            chars[i:close_end] = " " * (close_end - i)
            i = close_end
        out.append("".join(chars) + ending)

    return "".join(out)


def strip_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_inline_list(v):
    inner = v.strip().lstrip("[").rstrip("]")
    return [strip_scalar(x) for x in inner.split(",") if x.strip()]


def parse_frontmatter(text):
    """Minimal YAML-frontmatter parser: scalars, inline lists [a, b], block lists (- item)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end]
    body = text[end + 4:]
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


def is_excluded(path: Path, vault: Path, rules):
    prefixes = tuple(rules["scan"]["exclude_dir_prefixes"])
    names = set(rules["scan"]["exclude_dirs"])
    for part in path.relative_to(vault).parts[:-1]:
        if part in names or part.startswith(prefixes):
            return True
    return False


def first_h1(body):
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def main():
    ap = argparse.ArgumentParser(description="Integrity gate for a Harness KB.")
    ap.add_argument("vault", help="path to the vault (folder of Markdown notes)")
    ap.add_argument("--rules", help="path to a rules JSON file (single source of truth)")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"error: not a directory: {vault}", file=sys.stderr)
        return 2

    rules = DEFAULT_RULES
    if args.rules:
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        for k, v in DEFAULT_RULES.items():
            rules.setdefault(k, v)

    img_exts = tuple(e.lower() for e in rules["attachments"]["image_extensions"])
    bin_exts = tuple(e.lower() for e in rules["attachments"]["binary_extensions_requiring_digest"])
    vocab = set(rules["tags"]["controlled_vocabulary"])
    index_tag = rules["tags"]["index_only_tag"]
    required = rules["frontmatter"]["required"]

    # Index every file so wikilinks can resolve by basename or by relative path.
    all_files, notes = [], []
    for p in vault.rglob("*"):
        if not p.is_file() or is_excluded(p, vault, rules):
            continue
        all_files.append(p)
        if p.suffix.lower() == ".md":
            notes.append(p)

    by_stem = {}
    for p in all_files:
        by_stem.setdefault(p.stem, []).append(p)
    rel_lookup = {str(p.relative_to(vault)).replace("\\", "/").lower(): p for p in all_files}

    referenced_images = set()
    heading_cache = {}
    problems = []  # (severity, note_rel, message)

    def resolve(target, note):
        target = target.split("#", 1)[0].split("|", 1)[0].strip()
        if not target:
            return note  # pure anchor, same note
        norm = target.replace("\\", "/")
        low = norm.lower()
        if low in rel_lookup:
            return rel_lookup[low]
        for cand in (low, low + ".md"):
            if cand in rel_lookup:
                return rel_lookup[cand]
        stem = Path(norm).name
        stem = stem[:-3] if stem.lower().endswith(".md") else stem
        hits = by_stem.get(stem) or by_stem.get(Path(norm).stem)
        return hits[0] if hits else None

    def headings_of(path):
        if path not in heading_cache:
            _, b = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            b = strip_markdown_code(b)
            heading_cache[path] = {
                re.sub(r"^#+\s+", "", ln).strip().lower()
                for ln in b.splitlines() if re.match(r"^#{1,6}\s", ln)
            }
        return heading_cache[path]

    for note in notes:
        rel = str(note.relative_to(vault)).replace("\\", "/")
        text = note.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        structure_body = strip_markdown_code(body)
        gate_ignore = str(meta.get("gate_ignore", "")).lower() == "true"
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        is_index = ("index" in tags) or str(meta.get("type", "")).lower() == "index" or note.stem.lower().startswith("index")

        # --- wikilinks & image embeds ---
        for embed, target in WIKILINK.findall(structure_body):
            dest = resolve(target, note)
            clean = target.split("#", 1)[0].split("|", 1)[0].strip()
            anchor = target.split("#", 1)[1].split("|", 1)[0].strip() if "#" in target else ""
            if embed == "!":
                if dest is None or dest.suffix.lower() not in img_exts:
                    problems.append(("ERR", rel, f"broken image embed: [[{target}]]"))
                else:
                    referenced_images.add(dest)
            else:
                if clean and dest is None:
                    problems.append(("ERR", rel, f"broken note link: [[{target}]]"))
                elif dest is not None and dest.suffix.lower() in img_exts:
                    referenced_images.add(dest)
                elif anchor and not anchor.startswith("^") and dest is not None and dest.suffix.lower() == ".md":
                    if anchor.lower() not in headings_of(dest):
                        problems.append(("ERR", rel, f"broken anchor: [[{target}]] (no heading '{anchor}')"))

        if gate_ignore:
            continue

        # --- frontmatter contract ---
        for field in required:
            if not meta.get(field):
                problems.append(("ERR", rel, f"missing required frontmatter: {field}"))

        # --- title == filename == H1 ---
        title = meta.get("title")
        if title and title != note.stem:
            problems.append(("WARN", rel, f"title != filename ('{title}' vs '{note.stem}')"))
        h1 = first_h1(structure_body)
        if title and h1 and h1 != title:
            problems.append(("WARN", rel, f"H1 != title ('{h1}' vs '{title}')"))

        # --- tag rules ---
        if is_index:
            extra = [t for t in tags if t != index_tag]
            if extra:
                problems.append(("ERR", rel, f"index file must have only the '{index_tag}' tag; also has {extra}"))
        elif vocab:
            for t in tags:
                if t not in vocab:
                    problems.append(("ERR", rel, f"tag '{t}' not in controlled vocabulary"))

        # --- binary attachment digest ("open the shrink-wrap") ---
        digest = meta.get("file_digest", [])
        if isinstance(digest, str):
            digest = [digest]
        att = note.parent / "attachments"
        if att.is_dir():
            for f in att.iterdir():
                if f.is_file() and f.suffix.lower() in bin_exts and f.name not in digest:
                    problems.append(("ERR", rel, f"binary attachment not in file_digest: {f.name}"))

    # --- orphan images (in any attachments/ folder, never referenced) ---
    for p in all_files:
        if p.suffix.lower() in img_exts and p.parent.name == "attachments" and p not in referenced_images:
            problems.append(("WARN", str(p.relative_to(vault)).replace("\\", "/"), "orphan image (unreferenced)"))

    errors = [p for p in problems if p[0] == "ERR"]
    warns = [p for p in problems if p[0] == "WARN"]

    print(f"Vault: {vault}")
    print(f"Notes checked: {len(notes)} | files: {len(all_files)}")
    print(f"Errors: {len(errors)} | Warnings: {len(warns)}\n")
    for sev, rel, msg in problems:
        print(f"  [{sev}] {rel}: {msg}")
    if not problems:
        print("  clean - 0 problems")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
