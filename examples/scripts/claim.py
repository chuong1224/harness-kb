#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claim.py - a mechanical per-file lock for a knowledge base several agents write to.

THE PROBLEM. Once more than one agent session touches the same vault, two of them
eventually edit the same file at the same time and one silently clobbers the other. The
usual mitigation is a rule in the docs - "check the current version before you edit a
shared file". That is a *social contract*: it holds exactly as long as every model
remembers it, and nothing catches the moment one doesn't.

THE FIX (blueprint H4). Turn the rule into a lock the runtime enforces. A `PreToolUse`
hook runs this script before every write tool call:

  * nobody holds the file -> claim it silently (a single agent never notices the lock);
  * another live stream holds it -> **exit 2**, which blocks the tool call, and the
    reason printed on stderr goes back to the model.

DATA MODEL. Each stream owns exactly ONE file: `<vault>/.claims/claim-<host>-<stream>.json`,
listing the files it currently holds:

    {"stream": "a1b2c3d4", "host": "workstation", "agent": "claude",
     "updated": 1750000000.0,
     "files": {"ops/runbook.md": {"since": 1749999880.0, "touched": 1750000000.0,
                                  "why": "rewriting the rollback section"}}}

One writer per file is deliberate. Vaults commonly live in a cloud-synced folder (Dropbox,
iCloud, Google Drive); two machines appending to one shared lock file is how you manufacture
conflict copies and lost updates - the very failure this is meant to prevent. Writers
never touch each other's files, and conflicts are resolved from DATA instead of from
write ordering: among live holders, the earliest `since` wins.

Claiming is write-then-verify: check, write your claim, then re-read and arbitrate. Two
streams that claim at nearly the same moment compute the same winner on the second read,
and the loser withdraws its entry instead of both believing they hold the file.

EXPIRY (nothing to clean up by hand): a claim on a file goes stale after
KB_CLAIM_FILE_TTL seconds untouched; a stream that has been silent for
KB_CLAIM_STREAM_TTL is treated as dead and all its claims lapse; a `SessionEnd` hook
releases everything immediately.

KNOWN LIMITS - stated plainly, because a lock people over-trust is worse than none:
  * Across machines the guarantee is only as fast as your file sync (tens of seconds).
    It reliably prevents *sustained* collisions; it cannot serialise two writes a few
    seconds apart on two machines. On one machine it is solid.
  * It only covers the agent's file tools. Writes through a shell, a script or a desktop
    editor never reach the hook - by design, so a human editing their own notes is never
    blocked. Scripts that touch shared files should `take` and `release` explicitly.
  * It fails OPEN on anything unexpected (malformed payload, no stream id, no vault
    found). A broken lock must never freeze an agent session; it may only block when it
    is certain someone else holds the file.

USAGE
    claim.py hook                  # PreToolUse: reads the hook JSON on stdin
    claim.py end                   # SessionEnd: release everything this session held
    claim.py status [--json]       # who holds what
    claim.py check <path>          # exit 0 = writable, 1 = held by another stream
    claim.py take <path> [--why]   # explicit claim, for scripts and non-hook agents
    claim.py release <path> | --all
    claim.py steal <path> [--why]  # take over from a stream you know is dead (logged)
    claim.py gc                    # drop claim files of long-dead streams

Common flags: --stream ID (default $KB_CLAIM_STREAM) - --vault PATH - --json
Escape hatch: KB_CLAIM_OFF=1 disables the gate entirely.

Exit codes: 0 = allowed / command ok - 1 = blocked (or command failed) - 2 = HOOK BLOCK.
Python 3.8+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")          # non-ASCII note names on Windows consoles
    except Exception:                             # noqa: BLE001 - older Python / odd stream
        pass

HOST = socket.gethostname().split(".")[0] or "unknown"

STREAM_TTL = int(os.environ.get("KB_CLAIM_STREAM_TTL") or 45 * 60)
FILE_TTL = int(os.environ.get("KB_CLAIM_FILE_TTL") or 20 * 60)
GC_AGE = int(os.environ.get("KB_CLAIM_GC_AGE") or 24 * 3600)

GATED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Vault root markers, in order. Add your own with KB_CLAIM_ROOT_MARKER.
ROOT_MARKERS = [m for m in (os.environ.get("KB_CLAIM_ROOT_MARKER"), ".obsidian", ".kb-root") if m]

# Directories the lock ignores: its own state, VCS internals, editor config, build junk.
SKIP_SEGMENTS = {".git", ".obsidian", ".claims", ".trash", "__pycache__", "node_modules", "backup"}
# Machine-written runtime files. Gating these is pure noise: they are appended to by
# tooling, not edited by two agents racing over the same paragraph.
SKIP_NAME_RE = re.compile(r"(?i)^(activity.*\.jsonl|.*\.lock|.*\.bak-.*|.*\.tmp-\d+)$")


# ----------------------------------------------------------------------------- io

def die(msg: str):
    print("error: " + msg, file=sys.stderr)
    sys.exit(2)


def off() -> bool:
    return (os.environ.get("KB_CLAIM_OFF") or "").strip() not in ("", "0", "false", "no")


def find_vault(start: Path):
    """Walk up looking for a vault root marker. Returns None rather than guessing."""
    try:
        cands = [start, *start.parents]
    except (OSError, ValueError):
        return None
    for cand in cands:
        for marker in ROOT_MARKERS:
            try:
                if (cand / marker).exists():
                    return cand
            except OSError:
                continue
    return None


def claims_dir(vault: Path) -> Path:
    d = os.environ.get("KB_CLAIM_DIR")
    return Path(d) if d else vault / ".claims"


def slug(s) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s))[:48] or "x"


def rec_file(cdir: Path, host: str, stream: str) -> Path:
    return cdir / ("claim-%s-%s.json" % (slug(host), slug(stream)))


def load_records(cdir: Path):
    """Read every claim file. A corrupt or half-synced one is skipped, never fatal."""
    out = []
    try:
        paths = sorted(cdir.glob("claim-*.json"))
    except OSError:
        return out
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("stream"):
            d["_path"] = p
            d.setdefault("files", {})
            out.append(d)
    return out


def save_record(rec: dict) -> None:
    """Atomically write this stream's own claim file (per-process temp + os.replace)."""
    p = rec["_path"]
    body = {k: v for k, v in rec.items() if not k.startswith("_")}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))


def log_event(cdir: Path, kind: str, **kw) -> None:
    """Append-only trail of the RARE events (a block, a steal) - the evidence that the
    lock did something. Routine claims are not logged; they would drown it."""
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(ts=round(time.time(), 3), host=HOST, kind=kind, **kw),
                          ensure_ascii=False)
        with open(cdir / ("_events-%s.jsonl" % slug(HOST)), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _acquire(lf) -> None:
    """Cross-process lock around this stream's own file, for parallel tool calls in one
    session. Best effort: if it can't be had in ~1s, proceed anyway."""
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            for _ in range(100):
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    except Exception:                             # noqa: BLE001
        pass


def _release(lf) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:                             # noqa: BLE001
        pass


# -------------------------------------------------------------------------- locking

def key_of(vault: Path, target: str):
    """Contention key = vault-relative path, forward slashes, lowercased. None = not gated.

    Compared with normcase rather than Path.relative_to on purpose: the hook receives a
    path from the agent, and a drive letter in the other case ("D:" vs "d:") would raise
    ValueError and switch the lock off exactly when it is needed."""
    try:
        p = Path(target)
        if not p.is_absolute():
            p = Path.cwd() / p
        ap = os.path.normpath(str(p))
        vp = os.path.normpath(str(vault))
        if not os.path.normcase(ap).startswith(os.path.normcase(vp) + os.sep):
            return None                           # outside the vault - not our business
        rel = ap[len(vp) + 1:]
    except (ValueError, OSError):
        return None
    parts = tuple(x for x in re.split(r"[\\/]+", rel) if x)
    if not parts:
        return None
    if any(seg in SKIP_SEGMENTS for seg in parts):
        return None
    if SKIP_NAME_RE.match(parts[-1]):
        return None
    return "/".join(parts).lower()


def holders(records, key: str, now: float):
    """[(since, stream, record, entry)] of LIVE streams holding `key`, earliest first."""
    out = []
    for r in records:
        if now - float(r.get("updated") or 0) > STREAM_TTL:        # stream is dead
            continue
        ent = (r.get("files") or {}).get(key)
        if not isinstance(ent, dict):
            continue
        if now - float(ent.get("touched") or 0) > FILE_TTL:        # claim went stale
            continue
        out.append((float(ent.get("since") or 0), str(r.get("stream")), r, ent))
    return sorted(out, key=lambda t: (t[0], t[1]))


def owner_of(records, key: str, now: float):
    h = holders(records, key, now)
    return h[0] if h else None


def my_record(cdir: Path, stream: str, agent: str) -> dict:
    p = rec_file(cdir, HOST, stream)
    rec = None
    for r in load_records(cdir):
        if r["_path"] == p:
            rec = r
            break
    if rec is None:
        now = round(time.time(), 3)
        rec = {"stream": stream, "host": HOST, "agent": agent, "pid": os.getpid(),
               "started": now, "updated": now, "files": {}, "_path": p}
    rec["agent"] = agent or rec.get("agent") or "?"
    rec["pid"] = os.getpid()
    return rec


def gc(cdir: Path, now: float) -> int:
    """Delete claim files of long-dead streams. Runs piggybacked on claiming, so it has
    to stay cheap and silent."""
    n = 0
    for r in load_records(cdir):
        if now - float(r.get("updated") or 0) > GC_AGE:
            try:
                r["_path"].unlink()
                n += 1
            except OSError:
                pass
    return n


def take(cdir: Path, stream: str, agent: str, key: str, why: str = "", steal: bool = False):
    """Claim write access to `key`. Returns (allowed, holder)."""
    now = time.time()
    cdir.mkdir(parents=True, exist_ok=True)
    recs = load_records(cdir)
    own = owner_of(recs, key, now)
    if own and own[1] != stream and not steal:
        drop(cdir, stream, key)                   # not the owner - don't leave a stale entry
        return False, own

    since = now
    if own and own[1] == stream:
        since = own[0]                            # renewal keeps the original timestamp
    h = holders(recs, key, now)
    if steal and h:
        # Taking over = declaring an EARLIER `since`. Deliberately not editing the other
        # stream's file: "only ever write your own file" is what keeps a synced folder
        # from producing conflict copies. The arbiter is still the data.
        since = min(since, h[0][0] - 1.0)

    lf = None
    ent = {}
    try:
        try:
            lf = open(str(rec_file(cdir, HOST, stream)) + ".lock", "a+")
            _acquire(lf)
        except OSError:
            lf = None
        rec = my_record(cdir, stream, agent)
        ent = dict(rec["files"].get(key) or {})
        ent.update(since=round(since, 3), touched=round(now, 3))
        if why:
            ent["why"] = why
        if steal and h:
            ent["stolen_from"] = "%s/%s" % (h[0][2].get("host"), h[0][1])
        rec["files"][key] = ent
        rec["updated"] = round(now, 3)
        save_record(rec)
    finally:
        if lf:
            _release(lf)
            lf.close()

    own2 = owner_of(load_records(cdir), key, now)
    if own2 and own2[1] != stream:
        drop(cdir, stream, key)                   # lost the arbitration - withdraw cleanly
        return False, own2
    if steal:
        log_event(cdir, "steal", stream=stream, key=key, frm=ent.get("stolen_from"), why=why)
    gc(cdir, now)
    return True, None


def drop(cdir: Path, stream: str, key) -> int:
    """Release one file, or everything this stream holds when key is None."""
    p = rec_file(cdir, HOST, stream)
    if not p.is_file():
        return 0
    for r in load_records(cdir):
        if r["_path"] != p:
            continue
        if key is None:
            n = len(r.get("files") or {})
            try:
                p.unlink()
            except OSError:
                return 0
            try:
                Path(str(p) + ".lock").unlink()
            except OSError:
                pass
            return n
        if key in (r.get("files") or {}):
            del r["files"][key]
            r["updated"] = round(time.time(), 3)
            save_record(r)
            return 1
    return 0


# ------------------------------------------------------------------------ reporting

def ago(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 60:
        return "%ds ago" % int(sec)
    if sec < 3600:
        return "%dm ago" % int(sec // 60)
    if sec < 86400:
        return "%dh ago" % int(sec // 3600)
    return "%dd ago" % int(sec // 86400)


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def block_message(key: str, holder, now: float, script: str) -> str:
    since, stream, rec, ent = holder
    why = (" - reason: " + ent["why"]) if ent.get("why") else ""
    return "\n".join([
        'CLAIM LOCK: "%s" is held by another stream.' % key,
        "  held by : %s / %s / stream %s%s" % (rec.get("host"), rec.get("agent"), stream, why),
        "  since   : %s (%s), last touched %s" % (
            clock(since), ago(now - since), ago(now - float(ent.get("touched") or since))),
        "Do not route around this. Pick one:",
        "  * wait and retry - the claim lapses after %d minutes untouched" % (FILE_TTL // 60),
        '  * see the whole picture:  python "%s" status' % script,
        '  * that stream is dead:    python "%s" steal "%s" --why "..."' % (script, key),
        "  (emergency: set KB_CLAIM_OFF=1 to disable the lock, and tell your team)",
    ])


def cmd_status(args, vault: Path, cdir: Path) -> int:
    now = time.time()
    rows = []
    for r in sorted(load_records(cdir), key=lambda r: -float(r.get("updated") or 0)):
        alive = now - float(r.get("updated") or 0) <= STREAM_TTL
        files = []
        for k, e in sorted((r.get("files") or {}).items()):
            hot = now - float(e.get("touched") or 0) <= FILE_TTL
            files.append({"file": k, "since": e.get("since"), "touched": e.get("touched"),
                          "why": e.get("why", ""), "held": hot and alive})
        rows.append({"stream": r.get("stream"), "host": r.get("host"), "agent": r.get("agent"),
                     "updated": r.get("updated"), "alive": alive, "files": files})
    if args.json:
        print(json.dumps({"now": now, "stream_ttl": STREAM_TTL, "file_ttl": FILE_TTL,
                          "streams": rows}, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("No stream holds anything (%s is empty)." % cdir)
        return 0
    for r in rows:
        print("%s %s / %s / stream %s / seen %s" % (
            "[live]" if r["alive"] else "[dead]", r["host"], r["agent"], r["stream"],
            ago(now - float(r["updated"] or 0))))
        for f in r["files"]:
            print("   %s %s  (since %s, touched %s)%s" % (
                "HELD" if f["held"] else "----", f["file"], clock(float(f["since"] or 0)),
                ago(now - float(f["touched"] or 0)), (" - " + f["why"]) if f["why"] else ""))
    print("\nThresholds: stream dies after %dm silent, claim lapses after %dm untouched."
          % (STREAM_TTL // 60, FILE_TTL // 60))
    return 0


# ------------------------------------------------------------------------- commands

def resolve_target(vault: Path, target: str):
    p = Path(target)
    if not p.is_absolute():
        p = vault / target                        # accept the relative key `status` prints
    k = key_of(vault, str(p))
    return (p, k) if k else None


def cmd_take(args, vault: Path, cdir: Path) -> int:
    r = resolve_target(vault, args.path)
    if not r:
        print("Not gated: %s" % args.path)
        return 0
    ok, holder = take(cdir, args.stream, args.agent, r[1], args.why or "",
                      steal=(args.cmd == "steal"))
    if ok:
        print("%s: %s" % ("STOLEN" if args.cmd == "steal" else "CLAIMED", r[1]))
        return 0
    print(block_message(r[1], holder, time.time(), __file__), file=sys.stderr)
    return 1


def cmd_check(args, vault: Path, cdir: Path) -> int:
    r = resolve_target(vault, args.path)
    if not r:
        print("Not gated - write freely.")
        return 0
    own = owner_of(load_records(cdir), r[1], time.time())
    if not own or own[1] == args.stream:
        print("Writable: %s" % r[1])
        return 0
    print(block_message(r[1], own, time.time(), __file__), file=sys.stderr)
    return 1


def cmd_release(args, vault: Path, cdir: Path) -> int:
    if args.all:
        print("Released %d file(s) held by stream %s." % (drop(cdir, args.stream, None), args.stream))
        return 0
    if not args.path:
        die("release needs <path> or --all")
    r = resolve_target(vault, args.path)
    if not r:
        print("Not gated - nothing to release.")
        return 0
    print("Release %s: %s" % (r[1], "ok" if drop(cdir, args.stream, r[1]) else "was not held"))
    return 0


def cmd_gc(args, vault: Path, cdir: Path) -> int:
    print("Removed %d claim file(s) of streams silent for more than %dh."
          % (gc(cdir, time.time()), GC_AGE // 3600))
    return 0


def read_hook_payload() -> dict:
    """Read the payload as BYTES and decode UTF-8 explicitly.

    Not sys.stdin.read(): on Windows a child process gets its stdin encoding from the
    locale (cp1252), while the agent writes UTF-8 - so a path with non-ASCII characters
    comes back mangled. That is not merely ugly, it CHANGES THE CONTENTION KEY: the hook
    and the CLI then compute different keys for the same file and the lock quietly stops
    protecting exactly those notes. Found the hard way."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:                             # noqa: BLE001 - wrapped/closed stdin
        try:
            raw = sys.stdin.read()
        except Exception:                         # noqa: BLE001
            return {}
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def cmd_hook(args) -> int:
    """PreToolUse. Fails OPEN on anything unexpected: a broken lock must not freeze a
    session; it may only block when it is certain another stream holds the file."""
    if off():
        return 0
    d = read_hook_payload()
    if (d.get("tool_name") or "") not in GATED_TOOLS:
        return 0
    ti = d.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    stream = (d.get("session_id") or "")[:12]
    if not target or not stream:
        return 0                                  # cannot identify the stream - let it pass
    if args.vault:
        vault = Path(args.vault)
    else:
        vault = find_vault(Path(os.path.normpath(os.path.join(os.getcwd(), str(target)))))
        if not vault and d.get("cwd"):
            vault = find_vault(Path(str(d["cwd"])))
    if not vault:
        return 0
    key = key_of(vault, str(target))
    if not key:
        return 0
    cdir = claims_dir(vault)
    agent = os.environ.get("KB_CLAIM_AGENT") or "agent"
    ok, holder = take(cdir, stream, agent, key)
    if ok:
        return 0
    log_event(cdir, "block", stream=stream, key=key, by=holder[1], by_host=holder[2].get("host"))
    print(block_message(key, holder, time.time(), __file__), file=sys.stderr)
    return 2                                      # exit 2 blocks the tool call


def cmd_end(args) -> int:
    """SessionEnd - release immediately so the next stream doesn't wait out the TTL."""
    d = read_hook_payload()
    stream = (d.get("session_id") or "")[:12]
    if not stream:
        return 0
    vault = Path(args.vault) if args.vault else find_vault(Path(d.get("cwd") or os.getcwd()))
    if not vault:
        return 0
    drop(claims_dir(vault), stream, None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Mechanical per-file lock for multi-agent vaults.")
    ap.add_argument("cmd", choices=["hook", "end", "status", "check", "take", "release", "steal", "gc"])
    ap.add_argument("path", nargs="?", help="absolute path, or path relative to the vault")
    ap.add_argument("--stream", default=os.environ.get("KB_CLAIM_STREAM") or "manual")
    ap.add_argument("--agent", default=os.environ.get("KB_CLAIM_AGENT") or "agent")
    ap.add_argument("--why", default="", help="why you hold it - shown in the block message")
    ap.add_argument("--vault", default="", help="vault root (default: nearest marker dir)")
    ap.add_argument("--all", action="store_true", help="release: everything this stream holds")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook":
        try:
            return cmd_hook(args)
        except Exception as e:                    # noqa: BLE001 - fail open, never block wrongly
            print("claim.py hook error (allowing): %r" % (e,), file=sys.stderr)
            return 0
    if args.cmd == "end":
        try:
            return cmd_end(args)
        except Exception:                         # noqa: BLE001
            return 0

    vault = Path(args.vault).resolve() if args.vault else find_vault(Path.cwd())
    if not vault:
        die("no vault root found (looked for %s) - pass --vault PATH" % ", ".join(ROOT_MARKERS))
    cdir = claims_dir(vault)
    if args.cmd == "status":
        return cmd_status(args, vault, cdir)
    if args.cmd == "gc":
        return cmd_gc(args, vault, cdir)
    if args.cmd == "check":
        if not args.path:
            die("check needs <path>")
        return cmd_check(args, vault, cdir)
    if args.cmd == "release":
        return cmd_release(args, vault, cdir)
    if not args.path:
        die("%s needs <path>" % args.cmd)
    return cmd_take(args, vault, cdir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
