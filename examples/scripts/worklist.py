#!/usr/bin/env python3
"""Turn retrieval-health measurements into a deterministic review worklist.

This is the H3 boundary: consume one already-measured insight snapshot, propose
specific actions, and stop.  The module never edits, links, moves, or merges a
note.  Semantic changes remain supervised work.

Input is JSON with any of these optional sections::

    {
      "never": {"list": [{"file": "Research/New.md"}]},
      "unread": {"list": ["Research/New.md"]},
      "weak": {
        "orphans": {"list": ["Research/New.md"]},
        "no_index": {"list": ["Research/New.md"]},
        "index_candidates": {"Research/New.md": "Research/Index - Research.md"}
      },
      "friction": {
        "reread": {"list": [{"file": "Ops/Rules.md", "rereads": 3, "chains": 2}]},
        "long": {"list": [{"first": "A.md", "last": "F.md", "distinct": 6,
                              "count": 8, "span": 120, "agent": "agent"}]}
      },
      "taxonomy": {
        "b3_scope_leakage": {"list": [{"file": "A.md", "target": "B.md",
          "section": "Retries", "margin": 0.14,
          "own_similarity": 0.20, "other_similarity": 0.34}]}
      }
    }

The upstream sensor owns measurement and candidate selection.  In particular,
this script does not recompute retrieval chains or guess an index across areas.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REREAD_MIN = 2
LONG_CHAIN_MIN = 6
SCOPE_MARGIN_MIN = 0.08
WORKLIST_LIMIT = 30
REREAD_CAP = 8
LONG_CHAIN_CAP = 5
SCOPE_CAP = 10


def _stable_id(kind: str, source: str, target: str = "", extra: str = "") -> str:
    raw = "|".join((kind, source or "", target or "", extra or ""))
    return "H3-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _files(rows: Iterable[Any]) -> set[str]:
    out = set()
    for row in rows:
        if isinstance(row, str):
            out.add(row)
        elif isinstance(row, dict) and isinstance(row.get("file"), str):
            out.add(row["file"])
    return out


def build_worklist(snapshot: Dict[str, Any], limit: int = WORKLIST_LIMIT) -> Dict[str, Any]:
    """Return review-only proposals without mutating or remeasuring *snapshot*."""
    items: List[Dict[str, Any]] = []
    seen = set()

    def add(priority: str, kind: str, source: str,
            target: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None,
            extra: str = "") -> None:
        ident = _stable_id(kind, source, target or "", extra)
        if ident in seen:
            return
        seen.add(ident)
        items.append({
            "id": ident,
            "priority": priority,
            "kind": kind,
            "file": source,
            "target": target,
            "evidence": evidence or {},
            "review_required": True,
        })

    weak = snapshot.get("weak") or {}
    never = _files((snapshot.get("never") or {}).get("list", []))
    unread = _files((snapshot.get("unread") or {}).get("list", []))
    orphans = _files((weak.get("orphans") or {}).get("list", []))
    no_index = sorted(_files((weak.get("no_index") or {}).get("list", [])))
    candidates = weak.get("index_candidates") or {}
    covered = set()

    for source in no_index:
        target = candidates.get(source)
        blind = source in never or source in unread
        add(
            "P1" if blind or source in orphans else "P2",
            "connect_index" if target else "review_index",
            source,
            target,
            {"orphan": source in orphans, "never": source in never,
             "unread": source in unread},
        )
        covered.add(source)

    for source in sorted((never | unread) - covered):
        add("P1", "open_unseen", source,
            evidence={"never": source in never, "unread": source in unread})

    friction = snapshot.get("friction") or {}
    rereads = (friction.get("reread") or {}).get("list", [])
    rereads = sorted(rereads, key=lambda row: (
        -int(row.get("rereads", 0)), -int(row.get("chains", 0)), row.get("file", "")))
    for row in rereads[:REREAD_CAP]:
        if int(row.get("rereads", 0)) >= REREAD_MIN and row.get("file"):
            add("P2", "reduce_reread", row["file"], evidence={
                "rereads": int(row["rereads"]), "chains": int(row.get("chains", 0))})

    routes: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in (friction.get("long") or {}).get("list", []):
        if not row.get("first") or not row.get("last"):
            continue
        if int(row.get("distinct", 0)) < LONG_CHAIN_MIN:
            continue
        key = (row["first"], row["last"])
        current = routes.get(key)
        if current is None:
            current = copy.deepcopy(row)
            current["occurrences"] = 1
            routes[key] = current
        else:
            current["occurrences"] += 1
            current_score = (int(current.get("distinct", 0)), int(current.get("count", 0)),
                             float(current.get("span", 0)))
            row_score = (int(row.get("distinct", 0)), int(row.get("count", 0)),
                         float(row.get("span", 0)))
            if row_score > current_score:
                occurrences = current["occurrences"]
                current.clear()
                current.update(copy.deepcopy(row))
                current["occurrences"] = occurrences

    route_rows = sorted(routes.values(), key=lambda row: (
        -int(row["occurrences"]), -int(row.get("distinct", 0)),
        -int(row.get("count", 0)), row["first"], row["last"]))
    for row in route_rows[:LONG_CHAIN_CAP]:
        add("P2", "shorten_chain", row["first"], row["last"], {
            "distinct": int(row.get("distinct", 0)),
            "count": int(row.get("count", 0)),
            "span": float(row.get("span", 0)),
            "agent": row.get("agent") or "unknown",
            "occurrences": int(row["occurrences"]),
        })

    scope_rows = ((snapshot.get("taxonomy") or {}).get("b3_scope_leakage") or {}).get("list", [])
    scope_rows = sorted(scope_rows, key=lambda row: (
        -float(row.get("margin", 0)), row.get("file", ""), row.get("section", "")))
    scope_added = 0
    for row in scope_rows:
        if float(row.get("margin", 0)) < SCOPE_MARGIN_MIN or not row.get("file"):
            continue
        add("P2", "review_scope", row["file"], row.get("target"), {
            "section": row.get("section", ""),
            "margin": float(row.get("margin", 0)),
            "own_similarity": float(row.get("own_similarity", 0)),
            "other_similarity": float(row.get("other_similarity", 0)),
        }, extra=row.get("section", ""))
        scope_added += 1
        if scope_added >= SCOPE_CAP:
            break

    rank = {"P1": 0, "P2": 1, "P3": 2}
    items.sort(key=lambda item: (
        rank.get(item["priority"], 9), item["kind"], item["file"],
        item.get("target") or "", item["id"]))
    counts = Counter(item["priority"] for item in items)
    safe_limit = max(1, int(limit))
    shown = items[:safe_limit]
    return {
        "mode": "proposal_only",
        "auto_apply": False,
        "review_required": True,
        "total": len(items),
        "shown": len(shown),
        "truncated": len(items) > len(shown),
        "counts": {priority: counts.get(priority, 0) for priority in ("P1", "P2", "P3")},
        "thresholds": {
            "reread_min": REREAD_MIN,
            "long_chain_min_distinct": LONG_CHAIN_MIN,
            "scope_margin_min": SCOPE_MARGIN_MIN,
            "caps": {"reread": REREAD_CAP, "long_chain": LONG_CHAIN_CAP, "scope": SCOPE_CAP},
        },
        "items": shown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="retrieval-health snapshot JSON")
    parser.add_argument("--out", type=Path, help="write JSON here (stdout when omitted)")
    parser.add_argument("--limit", type=int, default=WORKLIST_LIMIT)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = build_worklist(snapshot, limit=args.limit)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        # Path.write_text gained ``newline`` after the repo's Python 3.8 floor.
        with args.out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
