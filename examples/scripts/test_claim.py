# -*- coding: utf-8 -*-
"""
test_claim.py - break-the-lock tests for claim.py.

A lock is only worth having if you can show it BLOCKS when it should and stays out of
the way when it shouldn't. Every case runs against a throwaway vault in the system temp
directory, so nothing here can touch real notes.

Time is not simulated with sleeps but by rewriting timestamps inside the claim files:
the whole suite runs in about two seconds and the expiry thresholds are checked exactly.

Run:   python examples/scripts/test_claim.py
Exit:  0 = all cases pass, 1 = at least one failed
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "claim.py"
HOST = socket.gethostname().split(".")[0] or "unknown"
STREAM_TTL, FILE_TTL = 3600, 600
TMP_PREFIX = "claim-test-"
failures = 0
vault = None
temp_root = None


def report(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok and detail:
        print("---- output ----\n" + detail.strip() + "\n----------------")


def own_temp(p):
    """Accept only paths owned by this exact test run; every deletion goes through here."""
    if p is None or temp_root is None:
        return False
    try:
        candidate = Path(p).resolve()
        owner = temp_root.resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    owner_is_safe = (
        owner != system_temp
        and system_temp in owner.parents
        and owner.name.startswith(TMP_PREFIX)
    )
    return owner_is_safe and (candidate == owner or owner in candidate.parents)


def wipe(p):
    """Delete only directories owned by this test run; reject everything else."""
    if own_temp(p):
        shutil.rmtree(p, ignore_errors=True)


def run(argv, stdin=None, env_extra=None):
    env = dict(os.environ)
    env["KB_CLAIM_STREAM_TTL"] = str(STREAM_TTL)
    env["KB_CLAIM_FILE_TTL"] = str(FILE_TTL)
    for k in ("KB_CLAIM_OFF", "KB_CLAIM_STREAM", "KB_CLAIM_DIR"):
        env.pop(k, None)
    env.update(env_extra or {})
    # stdin is fed as UTF-8 BYTES, the way the agent runtime feeds a hook. Text mode here
    # would let subprocess negotiate the encoding and hide the very bug case 12 exists for.
    p = subprocess.run([sys.executable, str(SCRIPT)] + argv,
                       input=(stdin or "").encode("utf-8") if stdin is not None else None,
                       capture_output=True, cwd=str(vault), env=env, timeout=60)
    dec = lambda b: (b or b"").decode("utf-8", "replace")   # noqa: E731
    return p.returncode, dec(p.stdout) + dec(p.stderr)


def hook(tool, path, session, env_extra=None, with_vault=True):
    # ensure_ascii=False on purpose: real payloads carry raw UTF-8, and an all-ASCII
    # payload cannot expose a decoding bug.
    payload = json.dumps({"session_id": session, "hook_event_name": "PreToolUse",
                          "cwd": str(vault), "tool_name": tool,
                          "tool_input": {"file_path": str(path)}}, ensure_ascii=False)
    argv = ["hook"] + (["--vault", str(vault)] if with_vault else [])
    return run(argv, stdin=payload, env_extra=env_extra)


def cdir():
    return vault / ".claims"


def rec_of(stream):
    return cdir() / ("claim-%s-%s.json" % (HOST, stream))


def events():
    p = cdir() / ("_events-%s.jsonl" % HOST)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def shift(stream, updated=None, touched=None):
    p = rec_of(stream)
    d = json.loads(p.read_text(encoding="utf-8"))
    now = time.time()
    if updated is not None:
        d["updated"] = now - updated
    for ent in d.get("files", {}).values():
        if touched is not None:
            ent["touched"] = now - touched
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def keys_of(stream):
    p = rec_of(stream)
    if not p.is_file():
        return []
    return sorted(json.loads(p.read_text(encoding="utf-8")).get("files", {}))


def reset():
    wipe(cdir())


def main() -> int:
    global vault, temp_root
    tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    temp_root = tmp.resolve()
    vault = tmp / "demo vault"                     # a space in the path, on purpose
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "ops").mkdir()
    shared = vault / "ops" / "runbook.md"
    shared.write_text("# runbook", encoding="utf-8")
    (vault / "notes.md").write_text("# notes", encoding="utf-8")
    try:
        # Destructive-delete fuse: inspect paths only; never delete the unsafe examples.
        report("S1. own_temp rejects the current directory",
               not own_temp(Path()) and not own_temp(Path.cwd()))
        report("S2. own_temp rejects the system temp root", not own_temp(tempfile.gettempdir()))
        report("S3. own_temp accepts this run's root and descendants",
               own_temp(tmp) and own_temp(vault) and own_temp(cdir()))
        report("S4. own_temp rejects a same-prefix directory from another run",
               not own_temp(Path(tempfile.gettempdir()) / "claim-test-not-this-run"))

        code, out = run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault),
                         "--why", "rewriting rollback"])
        report("1. claiming a free file succeeds", code == 0 and "CLAIMED" in out, out)

        code, out = run(["status", "--vault", str(vault)])
        report("2. status shows the holder and the reason",
               code == 0 and "runbook.md" in out and "rewriting rollback" in out, out)

        code, out = run(["take", "ops/runbook.md", "--stream", "B", "--vault", str(vault)])
        report("3. a second stream is BLOCKED on the same file",
               code == 1 and "CLAIM LOCK" in out and "rewriting rollback" in out, out)
        report("4. a blocked stream leaves no stale entry", keys_of("B") == [], str(keys_of("B")))

        code, out = run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        report("5. the holder can renew (never blocks itself)", code == 0, out)

        code, out = run(["take", "notes.md", "--stream", "B", "--vault", str(vault)])
        report("6. a different file is free (locking is per file, not per vault)", code == 0, out)

        shift("A", touched=FILE_TTL + 60)
        code, out = run(["take", "ops/runbook.md", "--stream", "B", "--vault", str(vault)])
        report("7. a claim untouched past FILE_TTL lapses", code == 0, out)
        reset()

        run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        shift("A", updated=STREAM_TTL + 60)        # silent stream = dead...
        shift("A", touched=1)                      # ...even with a freshly touched claim
        code, out = run(["check", "ops/runbook.md", "--stream", "B", "--vault", str(vault)])
        report("8. claims of a dead stream stop counting", code == 0, out)
        reset()

        run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault), "--why", "batch edit"])
        code, out = hook("Edit", shared, "B-session-xyz")
        report("9. hook Edit on a held file exits 2 with guidance",
               code == 2 and "CLAIM LOCK" in out and "steal" in out, out)
        report("10. the block is recorded in the event log", '"kind": "block"' in events(), events())

        code, out = hook("Edit", vault / "notes.md", "B-session-xyz")
        report("11. hook on a free file passes silently and claims it",
               code == 0 and out.strip() == "" and keys_of("B-session-xy") == ["notes.md"],
               out + str(keys_of("B-session-xy")))

        code, out = hook("Edit", vault / "ghi chú.md", "F-utf8")
        report("12. a non-ASCII path survives stdin intact (no mojibake key)",
               code == 0 and keys_of("F-utf8") == ["ghi chú.md"], out + repr(keys_of("F-utf8")))

        code, out = hook("Read", shared, "C-session")
        report("13. non-write tools are not gated", code == 0 and keys_of("C-session") == [], out)

        code, out = hook("Edit", cdir() / "claim-x.json", "C-session")
        report("14. the lock's own state directory is not gated",
               code == 0 and keys_of("C-session") == [], out)

        code, out = hook("Edit", tmp / "outside.md", "C-session")
        report("15. files outside the vault pass", code == 0 and keys_of("C-session") == [], out)

        code, out = run(["hook", "--vault", str(vault)], stdin="{ not json")
        report("16. a malformed payload fails OPEN", code == 0, out)

        code, out = hook("Edit", shared, "")
        report("17. no session id -> fail open, never guess", code == 0, out)

        code, out = hook("Edit", shared, "B-session-xyz", env_extra={"KB_CLAIM_OFF": "1"})
        report("18. KB_CLAIM_OFF=1 disables the gate", code == 0, out)

        code, out = hook("Edit", shared, "D-auto", with_vault=False)
        report("19. the hook finds the vault root by itself and still blocks",
               code == 2 and "CLAIM LOCK" in out, out)

        code, out = run(["steal", "ops/runbook.md", "--stream", "B", "--vault", str(vault),
                         "--why", "A is gone"])
        report("20. steal transfers ownership", code == 0 and "STOLEN" in out, out)
        code, out = run(["check", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        report("21. after a steal the roles are reversed", code == 1, out)

        code, out = run(["release", "--all", "--stream", "B", "--vault", str(vault)])
        code2, out2 = run(["check", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        report("22. release --all frees the file without waiting for the TTL",
               code == 0 and code2 == 0, out + out2)

        reset()
        hook("Edit", shared, "E-session")
        code, out = run(["end"], stdin=json.dumps({"session_id": "E-session", "cwd": str(vault),
                                                   "hook_event_name": "SessionEnd"}))
        report("23. SessionEnd releases everything the session held",
               code == 0 and not rec_of("E-session").is_file(), out)

        reset()
        run(["take", "notes.md", "--stream", "OLD", "--vault", str(vault)])
        shift("OLD", updated=48 * 3600)
        code, out = run(["gc", "--vault", str(vault)])
        report("24. gc removes claim files of long-dead streams",
               code == 0 and not rec_of("OLD").is_file(), out)

        reset()
        # Simulated race: B wrote an earlier `since` that A had not seen when it checked.
        # Only the re-read (write-then-verify) catches it, and A must withdraw.
        run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        now = time.time()
        rec_of("B").write_text(json.dumps({
            "stream": "B", "host": HOST, "agent": "agent", "updated": now,
            "files": {"ops/runbook.md": {"since": now - 30, "touched": now}},
        }, ensure_ascii=False), encoding="utf-8")
        code, out = run(["take", "ops/runbook.md", "--stream", "A", "--vault", str(vault)])
        report("25. in a close race the earlier claim wins and the loser withdraws",
               code == 1 and keys_of("A") == [], out + str(keys_of("A")))
    finally:
        wipe(tmp)
        vault = None
        temp_root = None

    print("\nRESULT: %s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
