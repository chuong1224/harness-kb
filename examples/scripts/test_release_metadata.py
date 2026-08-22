#!/usr/bin/env python3
"""Regression gate for release metadata and installable-source identity."""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SOURCE_RELEASE = json.loads((ROOT / "scaffold" / "release.json").read_text(encoding="utf-8"))
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
CLAUDE = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
RELEASE_BASE = "https://github.com/chuong1224/harness-kb/releases/tag/v"
ENTRY_RE = re.compile(r"(?m)^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$")
LINK_RE = re.compile(r"(?m)^\[(\d+\.\d+\.\d+)\]: (\S+)$")
BADGE_RE = re.compile(r"version-(\d+\.\d+\.\d+)-blue\.svg")
fails = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ((" -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


entries = ENTRY_RE.findall(CHANGELOG)
links = LINK_RE.findall(CHANGELOG)
link_map = dict(links)
badge = BADGE_RE.search(README)

check("changelog has release entries", bool(entries))
check("release entries are unique", len(entries) == len(set(entries)), entries)
check("release links are unique", len(links) == len(link_map), links)
check("release entries are newest first",
      [tuple(map(int, v.split("."))) for v in entries] ==
      sorted((tuple(map(int, v.split("."))) for v in entries), reverse=True), entries)

missing = sorted(set(entries) - set(link_map))
extra = sorted(set(link_map) - set(entries))
check("every release entry has one link definition", not missing, missing)
check("no release link exists without an entry", not extra, extra)

wrong = {version: url for version, url in links
         if url != RELEASE_BASE + version}
check("release links use the canonical tag URL", not wrong, wrong)
check("README badge equals newest changelog entry",
      bool(badge) and bool(entries) and badge.group(1) == entries[0],
      {"badge": badge.group(1) if badge else None,
       "newest": entries[0] if entries else None})
check("installable source version equals newest changelog entry",
      bool(entries) and SOURCE_RELEASE.get("version") == entries[0],
      {"source": SOURCE_RELEASE.get("version"), "newest": entries[0] if entries else None})
check("repo has one substantive multi-agent entrypoint",
      "## Before you commit" in AGENTS and "Read and follow `AGENTS.md`" in CLAUDE
      and len(CLAUDE.splitlines()) < 8)
check("lifecycle tool ships with its break-the-tool suite",
      (ROOT / "examples" / "scripts" / "harness.py").is_file()
      and (ROOT / "examples" / "scripts" / "test_harness.py").is_file())

print("\nSUMMARY:", ("FAIL %d checks" % len(fails)) if fails else "ALL PASS")
raise SystemExit(1 if fails else 0)
