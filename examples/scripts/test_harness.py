#!/usr/bin/env python3
"""Black-box lifecycle tests for scaffold, update delivery, upgrade and rollback."""

import contextlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FAILURES = []
ASSERTIONS = 0


def check(label, condition, detail=""):
    global ASSERTIONS
    ASSERTIONS += 1
    ok = bool(condition)
    print(("PASS " if ok else "FAIL ") + label + ((" -> " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(label)


def run(argv, cwd=None):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )


def text(proc):
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def copy_repo(source, target):
    def ignore(_path, names):
        return {name for name in names if name in {".git", "__pycache__", ".pytest_cache", ".test-tmp"}}

    shutil.copytree(source, target, ignore=ignore)


def git(repo, *args):
    proc = run(["git", "-C", repo, *args])
    if proc.returncode:
        raise RuntimeError("git %s failed:\n%s" % (" ".join(args), text(proc)))
    return proc.stdout.decode("utf-8", "replace").strip()


def commit_source(repo, message):
    git(repo, "init")
    git(repo, "config", "user.name", "Harness Test")
    git(repo, "config", "user.email", "harness-test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_base_source(temp):
    source = temp / "source-v1"
    copy_repo(ROOT, source)
    return source, commit_source(source, "Fixture release v1.20.0")


def make_new_source(old_source, temp, forced_red=False):
    source = temp / ("source-red" if forced_red else "source-v2")
    copy_repo(old_source, source)
    spec_path = source / "scaffold" / "release.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["version"] = "1.21.0"
    spec["migration_impact"] = ["Fixture migration: update one managed tool and add one tool."]
    if not forced_red:
        (source / "scaffold" / "new_tool.py").write_text(
            "#!/usr/bin/env python3\nprint('new managed tool')\n", encoding="utf-8", newline="\n"
        )
        spec["components"].append(
            {"source": "scaffold/new_tool.py", "target": ".harness/scripts/new_tool.py", "ownership": "upstream"}
        )
        with (source / "examples" / "scripts" / "generate_catalog.py").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n# lifecycle fixture v1.21.0\n")
        with (source / "scaffold" / "AGENTS.md").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\nUpstream template guidance added in v1.21.0.\n")
    else:
        spec["gates"].append(
            {"name": "forced-red", "argv": ["{python}", "-c", "raise SystemExit(7)"], "timeout": 10}
        )
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8", newline="\n")
    changelog = source / "CHANGELOG.md"
    old = changelog.read_text(encoding="utf-8")
    marker = "## [1.19.2]"
    entry = (
        "## [1.21.0] - 2026-08-22\n\n### Added\n"
        "- Fixture lifecycle release used by the black-box upgrade test.\n\n"
    )
    old = old.replace(marker, entry + marker, 1)
    old += "\n[1.21.0]: https://github.com/chuong1224/harness-kb/releases/tag/v1.21.0\n"
    changelog.write_text(old, encoding="utf-8", newline="\n")
    return source, commit_source(source, "Fixture release v1.21.0" + (" red" if forced_red else ""))


class ReleaseHandler(http.server.BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        payload = [
            {"tag_name": "v1.22.0", "html_url": "https://example/v1.22.0", "zipball_url": "", "published_at": "2026-08-24", "draft": False, "prerelease": False},
            {"tag_name": "v1.21.0", "html_url": "https://example/v1.21.0", "zipball_url": "", "published_at": "2026-08-23", "draft": False, "prerelease": False},
            {"tag_name": "v1.20.0", "html_url": "https://example/v1.20.0", "zipball_url": "", "published_at": "2026-08-22", "draft": False, "prerelease": False},
        ]
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


@contextlib.contextmanager
def release_server():
    ReleaseHandler.calls = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ReleaseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def test_workspace(scratch):
    """Use ordinary mkdir permissions; tempfile's 0700 ACL is hostile in some Windows sandboxes."""
    root = Path(scratch) / ("run-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def unused_local_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def newest_plan(vault, name=None):
    if name:
        return vault / ".harness" / "plans" / name
    return max((vault / ".harness" / "plans").glob("*.json"), key=lambda path: path.stat().st_mtime_ns)


def main():
    scratch = Path(os.environ.get("HARNESS_TEST_TMP") or (ROOT / ".test-tmp"))
    scratch.mkdir(parents=True, exist_ok=True)
    with test_workspace(scratch) as temp:
        source1, commit1 = make_base_source(temp)
        cli1 = source1 / "examples" / "scripts" / "harness.py"
        vault1 = temp / "vault-one"
        vault2 = temp / "vault-two"

        init1 = run([sys.executable, cli1, "init", vault1, "--source", source1, "--allow-network-checks"])
        check("init creates a runnable clean-vault scaffold", init1.returncode == 0, text(init1))
        init2 = run([sys.executable, cli1, "init", vault2, "--source", source1, "--no-network-checks"])
        check("a second vault initializes independently", init2.returncode == 0, text(init2))

        dirty_marker = source1 / "uncommitted-source.txt"
        dirty_marker.write_text("not represented by HEAD\n", encoding="utf-8", newline="\n")
        dirty = run([sys.executable, cli1, "init", temp / "dirty-target", "--source", source1, "--no-network-checks"])
        check("init rejects provenance that points at a dirty checkout", dirty.returncode == 2 and b"uncommitted bytes" in dirty.stderr, text(dirty))
        dirty_marker.unlink()

        check("AGENTS.md is the substantive entrypoint", "## Sources of truth" in (vault1 / "AGENTS.md").read_text(encoding="utf-8"))
        claude = (vault1 / "CLAUDE.md").read_text(encoding="utf-8")
        check("CLAUDE.md stays a thin pointer to AGENTS.md", "single human-owned" in claude and len(claude.splitlines()) < 12)
        check("scaffold copies no demo or private note data", not (vault1 / "Research").exists() and not (vault1 / "Ops").exists())

        manifest1 = json.loads((vault1 / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        manifest2 = json.loads((vault2 / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        check("manifest records exact installed version and commit", manifest1["installed"]["version"] == "1.20.0" and manifest1["installed"]["commit"] == commit1)
        check("manifest records ownership and base hash per component", all(item["ownership"] in {"user", "upstream"} and len(item["base_hash"]) == 64 for item in manifest1["components"]))
        check("two vaults never share lifecycle identity", manifest1["vault_id"] != manifest2["vault_id"])

        verify = run([sys.executable, vault1 / ".harness" / "harness.py", "verify", vault1])
        check("fresh install runs its real gates", verify.returncode == 0 and b"HARNESS VERIFIED" in verify.stdout, text(verify))

        disabled = run([sys.executable, vault2 / ".harness" / "harness.py", "check", vault2, "--api-base", "http://127.0.0.1:1", "--timeout", "0.2"])
        check("network is never contacted without recorded consent", disabled.returncode == 0 and b"disabled" in disabled.stdout.lower(), text(disabled))

        with release_server() as api:
            first = run([sys.executable, vault1 / ".harness" / "harness.py", "check", vault1, "--api-base", api, "--cache-seconds", "3600"])
            second = run([sys.executable, vault1 / ".harness" / "harness.py", "check", vault1, "--api-base", api, "--cache-seconds", "3600"])
            check("update delivery reports exact release distance", first.returncode == 0 and b"2 releases behind" in first.stdout, text(first))
            check("successful update checks are cached per vault", ReleaseHandler.calls == 1 and second.returncode == 0, {"calls": ReleaseHandler.calls, "output": text(second)})
        check("another vault did not inherit the first vault cache", not (vault2 / ".harness" / "cache" / "releases.json").exists())

        offline = run([
            sys.executable, vault1 / ".harness" / "harness.py", "check", vault1,
            "--api-base", "http://127.0.0.1:%d" % unused_local_port(), "--cache-seconds", "0", "--timeout", "0.2"
        ])
        offline_text = text(offline).lower()
        check("offline update check is silent and non-blocking", offline.returncode == 0 and "error" not in offline_text and "offline" not in offline_text, text(offline))

        custom_agents = (vault1 / "AGENTS.md").read_text(encoding="utf-8") + "\n# Local policy\nKeep this customization.\n"
        (vault1 / "AGENTS.md").write_text(custom_agents, encoding="utf-8", newline="\n")
        before_catalog = (vault1 / ".harness" / "scripts" / "generate_catalog.py").read_bytes()
        source2, commit2 = make_new_source(source1, temp)
        plan_path = vault1 / ".harness" / "plans" / "reviewed-v1.21.0.json"
        plan_run = run([
            sys.executable, vault1 / ".harness" / "harness.py", "plan", vault1,
            "--source", source2, "--out", plan_path
        ])
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
        actions = {item["path"]: item["action"] for item in plan.get("changes", [])}
        check("upgrade is planned before any managed byte changes", plan_run.returncode == 0 and (vault1 / ".harness" / "scripts" / "generate_catalog.py").read_bytes() == before_catalog, text(plan_run))
        check("plan preserves user-owned customization", actions.get("AGENTS.md") == "preserve-user", actions)
        check("plan distinguishes managed update and addition", actions.get(".harness/scripts/generate_catalog.py") == "update" and actions.get(".harness/scripts/new_tool.py") == "add", actions)
        check("plan carries migration impact and changelog range", bool(plan.get("migration_impact")) and [item["version"] for item in plan.get("release_notes", [])] == ["1.21.0"], plan)

        apply = run([sys.executable, vault1 / ".harness" / "harness.py", "apply", vault1, "--plan", plan_path])
        installed2 = json.loads((vault1 / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        check("reviewed upgrade applies and runs gates", apply.returncode == 0 and installed2["installed"] == {"version": "1.21.0", "commit": commit2, "at": installed2["installed"]["at"]}, text(apply))
        check("upgrade never overwrites AGENTS.md customization", (vault1 / "AGENTS.md").read_text(encoding="utf-8") == custom_agents)
        check("upgrade installs managed update and new component", (vault1 / ".harness" / "scripts" / "generate_catalog.py").read_bytes() != before_catalog and (vault1 / ".harness" / "scripts" / "new_tool.py").is_file())
        backup_id = plan["plan_id"]
        check("upgrade leaves a persistent backup and rollback handle", (vault1 / ".harness" / "backups" / backup_id / "backup.json").is_file() and backup_id in text(apply))

        rolled = run([sys.executable, vault1 / ".harness" / "harness.py", "rollback", vault1, "--backup", backup_id])
        restored = json.loads((vault1 / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        check("manual rollback restores old manifest and bytes", rolled.returncode == 0 and restored["installed"]["version"] == "1.20.0" and (vault1 / ".harness" / "scripts" / "generate_catalog.py").read_bytes() == before_catalog and not (vault1 / ".harness" / "scripts" / "new_tool.py").exists(), text(rolled))
        check("rollback also preserves user-owned customization", (vault1 / "AGENTS.md").read_text(encoding="utf-8") == custom_agents)

        tamper_path = vault1 / ".harness" / "plans" / "tampered.json"
        tamper_plan = run([
            sys.executable, vault1 / ".harness" / "harness.py", "plan", vault1,
            "--source", source2, "--out", tamper_path
        ])
        tampered = json.loads(tamper_path.read_text(encoding="utf-8"))
        for item in tampered["changes"]:
            if item["path"] == ".harness/scripts/generate_catalog.py":
                item["action"] = "noop"
        tamper_path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8", newline="\n")
        rejected = run([sys.executable, vault1 / ".harness" / "harness.py", "apply", vault1, "--plan", tamper_path])
        check("apply recomputes decisions and rejects a tampered plan", tamper_plan.returncode == 0 and rejected.returncode == 2 and b"plan decisions" in rejected.stderr, text(rejected))

        managed = vault1 / ".harness" / "scripts" / "generate_catalog.py"
        managed.write_bytes(before_catalog + b"\n# local managed edit\n")
        conflict_path = vault1 / ".harness" / "plans" / "conflict.json"
        conflict = run([
            sys.executable, vault1 / ".harness" / "harness.py", "plan", vault1,
            "--source", source2, "--out", conflict_path
        ])
        conflict_doc = json.loads(conflict_path.read_text(encoding="utf-8"))
        conflict_action = {item["path"]: item for item in conflict_doc["changes"]}[".harness/scripts/generate_catalog.py"]
        check("three-way plan refuses upstream/local double change", conflict.returncode == 2 and conflict_action["conflict"], text(conflict))
        managed.write_bytes(before_catalog)

        red_source, _red_commit = make_new_source(source1, temp, forced_red=True)
        red_plan = vault1 / ".harness" / "plans" / "forced-red.json"
        planned_red = run([
            sys.executable, vault1 / ".harness" / "harness.py", "plan", vault1,
            "--source", red_source, "--out", red_plan
        ])
        red_doc = json.loads(red_plan.read_text(encoding="utf-8")) if red_plan.exists() else {}
        applied_red = run([sys.executable, vault1 / ".harness" / "harness.py", "apply", vault1, "--plan", red_plan]) if planned_red.returncode == 0 else planned_red
        after_red = json.loads((vault1 / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        check("red post-upgrade gate triggers automatic rollback", planned_red.returncode == 0 and applied_red.returncode == 1 and b"ROLLED BACK" in applied_red.stdout and after_red["installed"]["version"] == "1.20.0", text(applied_red))
        check("automatic rollback keeps its forensic backup", bool(red_doc) and (vault1 / ".harness" / "backups" / red_doc["plan_id"] / "backup.json").is_file())

        final_verify = run([sys.executable, vault1 / ".harness" / "harness.py", "verify", vault1])
        check("full lifecycle leaves the original harness green", final_verify.returncode == 0, text(final_verify))

    print("\nSUMMARY: %s" % ("ALL PASS (%d assertions)" % ASSERTIONS if not FAILURES else "FAIL %d/%d" % (len(FAILURES), ASSERTIONS)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
