#!/usr/bin/env python3
"""Break-the-contract tests for the H3 review worklist."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from worklist import build_worklist


HERE = Path(__file__).resolve().parent


def fixture():
    return {
        "never": {"list": [{"file": "Research/New.md"}, {"file": "Inbox.md"}]},
        "unread": {"list": ["Research/New.md"]},
        "weak": {
            "orphans": {"list": ["Research/New.md"]},
            "no_index": {"list": ["Research/New.md", "Ops/Loose.md"]},
            "index_candidates": {"Research/New.md": "Research/Index - Research.md"},
        },
        "friction": {
            "reread": {"list": [
                {"file": "Ops/Rules.md", "rereads": 3, "chains": 2},
                {"file": "Ops/Small.md", "rereads": 1, "chains": 1},
            ]},
            "long": {"list": [
                {"first": "A.md", "last": "F.md", "distinct": 6,
                 "count": 7, "span": 90, "agent": "alpha"},
                {"first": "A.md", "last": "F.md", "distinct": 8,
                 "count": 10, "span": 140, "agent": "beta"},
                {"first": "A.md", "last": "E.md", "distinct": 5,
                 "count": 8, "span": 80, "agent": "alpha"},
            ]},
        },
        "taxonomy": {"b3_scope_leakage": {"list": [
            {"file": "Research/A.md", "target": "Research/B.md", "section": "Retries",
             "margin": 0.14, "own_similarity": 0.20, "other_similarity": 0.34},
            {"file": "Research/C.md", "target": "Research/D.md", "section": "Small",
             "margin": 0.03, "own_similarity": 0.25, "other_similarity": 0.28},
        ]}},
    }


class WorklistTests(unittest.TestCase):
    def test_proposals_are_review_only_and_input_is_unchanged(self):
        source = fixture()
        before = copy.deepcopy(source)
        result = build_worklist(source)
        self.assertEqual(source, before)
        self.assertEqual(result["mode"], "proposal_only")
        self.assertFalse(result["auto_apply"])
        self.assertTrue(result["review_required"])
        self.assertTrue(all(item["review_required"] for item in result["items"]))

    def test_action_classes_thresholds_and_exact_target(self):
        result = build_worklist(fixture())
        kinds = {item["kind"] for item in result["items"]}
        self.assertEqual(kinds, {"connect_index", "review_index", "open_unseen",
                                 "reduce_reread", "shorten_chain", "review_scope"})
        connect = next(item for item in result["items"] if item["kind"] == "connect_index")
        self.assertEqual(connect["target"], "Research/Index - Research.md")
        self.assertEqual(connect["priority"], "P1")
        self.assertNotIn("Ops/Small.md", {item["file"] for item in result["items"]})
        self.assertNotIn("Research/C.md", {item["file"] for item in result["items"]})

    def test_duplicate_routes_collapse_to_strongest_evidence(self):
        result = build_worklist(fixture())
        routes = [item for item in result["items"] if item["kind"] == "shorten_chain"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["file"], "A.md")
        self.assertEqual(routes[0]["target"], "F.md")
        self.assertEqual(routes[0]["evidence"]["occurrences"], 2)
        self.assertEqual(routes[0]["evidence"]["distinct"], 8)

    def test_ids_and_output_are_stable(self):
        first = build_worklist(fixture())
        second = build_worklist(fixture())
        self.assertEqual(first, second)
        self.assertTrue(all(item["id"].startswith("H3-") for item in first["items"]))
        self.assertEqual(len({item["id"] for item in first["items"]}), first["shown"])

    def test_limit_reports_truncation_without_changing_total(self):
        full = build_worklist(fixture())
        short = build_worklist(fixture(), limit=2)
        self.assertEqual(short["total"], full["total"])
        self.assertEqual(short["shown"], 2)
        self.assertTrue(short["truncated"])

    def test_cli_writes_only_the_requested_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapshot = root / "snapshot.json"
            output = root / "worklist.json"
            snapshot.write_text(json.dumps(fixture()), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(HERE / "worklist.py"), str(snapshot), "--out", str(output)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(result["auto_apply"])
            self.assertEqual(sorted(path.name for path in root.iterdir()),
                             ["snapshot.json", "worklist.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
