#!/usr/bin/env python3
"""Install and safely upgrade a Harness KB inside the vault it serves.

The repository used to be a blueprint plus examples.  This file closes the consumer
loop: an explicit ``init`` creates a runnable vault, and a manifest records exactly
which release supplied each component.  Updates are opt-in, cached and quiet while
offline.  Upgrades are two-step (plan, then apply), preserve user-owned files, back up
every changed path, run the installed gates, and roll back on any red gate.

Standard library only.  The integrity gate installed by the scaffold still has the
repository's one documented dependency: PyYAML.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


SCHEMA_VERSION = 1
SOURCE_SPEC = Path("scaffold/release.json")
MANIFEST_REL = Path(".harness/manifest.json")
PLAN_DIR_REL = Path(".harness/plans")
BACKUP_DIR_REL = Path(".harness/backups")
CACHE_REL = Path(".harness/cache/releases.json")
DEFAULT_CACHE_SECONDS = 24 * 60 * 60
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CHANGELOG_RE = re.compile(
    r"(?ms)^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## \[|^\[\d+\.\d+\.\d+\]:|\Z)"
)


class HarnessError(Exception):
    """A user-facing contract failure, not a traceback-worthy programming error."""


def utc_now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(path.read_bytes())


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def load_json(path, label="JSON"):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HarnessError("%s not found: %s" % (label, path))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("cannot read %s %s: %s" % (label, path, exc))
    if not isinstance(value, dict):
        raise HarnessError("%s must be a JSON object: %s" % (label, path))
    return value


def semver(value):
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise HarnessError("expected a stable X.Y.Z version, got %r" % (value,))
    return tuple(int(part) for part in value.split("."))


def safe_relative(value, label):
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise HarnessError("%s must be a safe relative path: %r" % (label, value))
    return path


def is_within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def resolved_child(root, relative, label):
    rel = safe_relative(relative, label)
    result = (Path(root) / rel).resolve()
    if not is_within(result, root):
        raise HarnessError("%s escapes its root: %s" % (label, relative))
    return result, rel


def git_commit(source):
    try:
        proc = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc and proc.returncode == 0:
        value = proc.stdout.decode("ascii", "replace").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            try:
                status = subprocess.run(
                    ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HarnessError("cannot prove that the source checkout is clean") from exc
            if status.returncode != 0:
                raise HarnessError("cannot prove that the source checkout is clean")
            if status.stdout.strip():
                raise HarnessError(
                    "source checkout has uncommitted bytes; commit them before installing so provenance is exact"
                )
            return value
    hint = Path(source) / ".harness-source-commit"
    if hint.is_file():
        value = hint.read_text(encoding="ascii", errors="replace").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    raise HarnessError(
        "source commit is unavailable; use a Git checkout or provide a .harness-source-commit file"
    )


def validate_source(source):
    source = Path(source).resolve()
    spec_path = source / SOURCE_SPEC
    spec = load_json(spec_path, "release specification")
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError("unsupported release specification schema: %r" % spec.get("schema_version"))
    if spec.get("product") != "harness-kb":
        raise HarnessError("release specification product must be 'harness-kb'")
    semver(spec.get("version"))
    if not isinstance(spec.get("repository"), str) or not spec["repository"].startswith("https://"):
        raise HarnessError("release specification needs an https repository URL")
    components = spec.get("components")
    if not isinstance(components, list) or not components:
        raise HarnessError("release specification has no components")
    targets = set()
    normalized = []
    reserved = {str(MANIFEST_REL).replace("\\", "/")}
    for raw in components:
        if not isinstance(raw, dict):
            raise HarnessError("every component must be an object")
        src_path, src_rel = resolved_child(source, raw.get("source", ""), "component source")
        target_rel = safe_relative(raw.get("target", ""), "component target")
        target_key = target_rel.as_posix()
        if target_key in reserved or target_key.startswith(".harness/backups/") or target_key.startswith(".harness/plans/") or target_key.startswith(".harness/cache/"):
            raise HarnessError("component target is lifecycle-reserved: %s" % target_key)
        if target_key in targets:
            raise HarnessError("duplicate component target: %s" % target_key)
        targets.add(target_key)
        if raw.get("ownership") not in ("upstream", "user"):
            raise HarnessError("component %s ownership must be upstream or user" % target_key)
        if not src_path.is_file() or src_path.is_symlink():
            raise HarnessError("component source must be a regular non-symlink file: %s" % src_rel)
        normalized.append(
            {
                "source": src_rel.as_posix(),
                "target": target_key,
                "ownership": raw["ownership"],
                "source_hash": sha256_file(src_path),
            }
        )
    gates = spec.get("gates") or []
    if not isinstance(gates, list) or not gates:
        raise HarnessError("release specification must declare at least one acceptance gate")
    for gate in gates:
        if not isinstance(gate, dict) or not gate.get("name") or not isinstance(gate.get("argv"), list):
            raise HarnessError("each gate needs name and argv")
        if not gate["argv"] or not all(isinstance(item, str) and item for item in gate["argv"]):
            raise HarnessError("gate argv must be a non-empty string list")
    return source, spec, normalized, git_commit(source)


def manifest_path(vault):
    return Path(vault).resolve() / MANIFEST_REL


def source_checkout_from(script_path):
    start = Path(script_path).resolve().parent
    for candidate in (start,) + tuple(start.parents):
        if (candidate / SOURCE_SPEC).is_file():
            return candidate
    return None


def load_manifest(vault):
    path = manifest_path(vault)
    doc = load_json(path, "installed manifest")
    if doc.get("schema_version") != SCHEMA_VERSION or doc.get("product") != "harness-kb":
        raise HarnessError("unsupported or foreign installed manifest: %s" % path)
    semver((doc.get("installed") or {}).get("version"))
    commit = (doc.get("installed") or {}).get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise HarnessError("installed manifest has no exact 40-character source commit")
    return doc


def component_record(component, actual_hash=None):
    return {
        "path": component["target"],
        "source": component["source"],
        "ownership": component["ownership"],
        "base_hash": component["source_hash"],
        "installed_hash": actual_hash or component["source_hash"],
    }


def installed_manifest(spec, components, commit, consent, vault_id=None, preserved=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "product": "harness-kb",
        "vault_id": vault_id or uuid.uuid4().hex,
        "source": {"repository": spec["repository"]},
        "installed": {"version": spec["version"], "commit": commit, "at": utc_now()},
        "update_checks": {
            "network_consent": bool(consent),
            "cache_seconds": int(spec.get("default_cache_seconds", DEFAULT_CACHE_SECONDS)),
        },
        "components": components,
        "preserved_components": preserved or [],
        "gates": spec["gates"],
    }


def expand_gate_argv(argv, vault):
    return [item.replace("{python}", sys.executable).replace("{vault}", str(vault)) for item in argv]


def run_gates(vault, gates, quiet=False):
    vault = Path(vault).resolve()
    failures = []
    for gate in gates:
        argv = expand_gate_argv(gate["argv"], vault)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(vault),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=int(gate.get("timeout", 180)),
            )
            stdout = proc.stdout.decode("utf-8", "replace")
            stderr = proc.stderr.decode("utf-8", "replace")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, code = "", "timed out", 124
        except OSError as exc:
            stdout, stderr, code = "", "could not run: %s" % exc, 125
        if not quiet:
            print("GATE %s: %s" % (gate["name"], "PASS" if code == 0 else "FAIL (%s)" % code))
            if stdout.strip():
                print(stdout.rstrip())
            if stderr.strip():
                print(stderr.rstrip(), file=sys.stderr)
        if code != 0:
            failures.append({"name": gate["name"], "exit": code, "stdout": stdout, "stderr": stderr})
    return failures


def effective_entries(target):
    if not target.exists():
        return []
    return [item for item in target.iterdir() if item.name != ".git"]


def init_vault(args):
    source_arg = args.source or source_checkout_from(__file__)
    if source_arg is None:
        raise HarnessError("init needs --source pointing at a Harness KB checkout")
    source, spec, source_components, commit = validate_source(source_arg)
    vault = Path(args.vault).resolve()
    if vault.exists() and not vault.is_dir():
        raise HarnessError("target is not a directory: %s" % vault)
    entries = effective_entries(vault)
    if entries and not args.adopt:
        raise HarnessError(
            "target is not empty (apart from .git); re-run with --adopt only after reviewing collisions"
        )
    vault.mkdir(parents=True, exist_ok=True)
    if manifest_path(vault).exists():
        raise HarnessError("this vault already has a Harness KB manifest")

    writes = []
    records = []
    for component in source_components:
        src = source / Path(component["source"])
        dest, _ = resolved_child(vault, component["target"], "component target")
        data = src.read_bytes()
        if sha256_bytes(data) != component["source_hash"]:
            raise HarnessError("source component changed while init was reading it: %s" % component["source"])
        if dest.exists():
            if not dest.is_file() or dest.is_symlink():
                raise HarnessError("target collision is not a regular file: %s" % component["target"])
            existing = dest.read_bytes()
            if component["ownership"] == "upstream" and existing != data:
                raise HarnessError("refusing to overwrite upstream component collision: %s" % component["target"])
            actual = sha256_bytes(existing)
        else:
            writes.append((dest, data))
            actual = component["source_hash"]
        records.append(component_record(component, actual))

    consent = bool(args.allow_network_checks)
    manifest = installed_manifest(spec, records, commit, consent)
    snapshots = {}
    touched = [path for path, _ in writes] + [manifest_path(vault)]
    for path in touched:
        snapshots[path] = path.read_bytes() if path.is_file() else None
    try:
        for path, data in writes:
            atomic_write(path, data)
        write_json(manifest_path(vault), manifest)
        failures = run_gates(vault, manifest["gates"])
        if failures:
            raise HarnessError("scaffold gates failed; the target was restored")
    except Exception:
        for path, old in reversed(list(snapshots.items())):
            if old is None:
                if path.is_file():
                    path.unlink()
            else:
                atomic_write(path, old)
        raise

    print("INITIALIZED Harness KB v%s at %s" % (spec["version"], vault))
    print("Source commit: %s" % commit)
    print("Components: %d (%d upstream-owned, %d user-owned)" % (
        len(records),
        sum(1 for item in records if item["ownership"] == "upstream"),
        sum(1 for item in records if item["ownership"] == "user"),
    ))
    print("Network update checks: %s" % ("consented" if consent else "disabled"))
    print("Next: edit AGENTS.md for this vault, then run `python .harness/harness.py verify .`.")
    return 0


def github_repo_parts(url):
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?", str(url))
    if not match:
        raise HarnessError("automatic update checks currently require a canonical GitHub repository URL")
    return match.group(1), match.group(2)


def read_cache(path, max_age):
    try:
        doc = load_json(path, "update cache")
        age = time.time() - float(doc.get("fetched_unix", 0))
        if (
            age <= max_age
            and doc.get("source") == "github-tags"
            and isinstance(doc.get("releases"), list)
        ):
            return doc
    except (HarnessError, TypeError, ValueError):
        pass
    return None


def fetch_release_catalog(repository, api_base, timeout):
    owner, repo = github_repo_parts(repository)
    releases = []
    page = 1
    while True:
        url = api_base.rstrip("/") + "/repos/%s/%s/tags?per_page=100&page=%d" % (owner, repo, page)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "harness-kb-lifecycle"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("tag endpoint did not return a list")
        for item in payload:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("name", ""))
            version = tag[1:] if tag.startswith("v") else tag
            if not VERSION_RE.fullmatch(version):
                continue
            releases.append(
                {
                    "version": version,
                    "tag": tag,
                    "url": repository.rstrip("/") + "/releases/tag/" + tag,
                    "zipball_url": item.get("zipball_url", ""),
                    "published_at": "",
                }
            )
        if len(payload) < 100:
            break
        page += 1
        if page > 60:
            raise ValueError("cannot certify exact release distance beyond 6000 tags")
    releases.sort(key=lambda item: semver(item["version"]), reverse=True)
    return {
        "source": "github-tags",
        "fetched_at": utc_now(),
        "fetched_unix": time.time(),
        "releases": releases,
    }


def update_status(args):
    vault = Path(args.vault).resolve()
    manifest = load_manifest(vault)
    installed = manifest["installed"]["version"]
    print("Harness KB v%s (%s)" % (installed, manifest["installed"]["commit"][:12]))
    consent = bool((manifest.get("update_checks") or {}).get("network_consent"))
    if not consent:
        print("Network update checks are disabled; enable them with `consent --allow-network`.")
        return 0
    cache_seconds = args.cache_seconds
    if cache_seconds is None:
        cache_seconds = int((manifest.get("update_checks") or {}).get("cache_seconds", DEFAULT_CACHE_SECONDS))
    cache_path = vault / CACHE_REL
    catalog = read_cache(cache_path, max(0, cache_seconds))
    if catalog is None:
        try:
            catalog = fetch_release_catalog(manifest["source"]["repository"], args.api_base, args.timeout)
            write_json(cache_path, catalog)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            # Offline is deliberately not an alarm.  The installed harness remains usable,
            # and the next consented check retries after the cache window.
            return 0
    newer = [item for item in catalog["releases"] if semver(item["version"]) > semver(installed)]
    if newer:
        latest = newer[0]
        print(
            "UPDATE AVAILABLE: %d release%s behind (installed v%s, latest v%s)."
            % (len(newer), "" if len(newer) == 1 else "s", installed, latest["version"])
        )
        print("Clone/pull the source, then run `python .harness/harness.py plan . --source <repo>`." )
    elif args.verbose:
        print("No newer stable release is known.")
    return 0


def set_consent(args):
    vault = Path(args.vault).resolve()
    manifest = load_manifest(vault)
    manifest.setdefault("update_checks", {})["network_consent"] = bool(args.allow_network)
    write_json(manifest_path(vault), manifest)
    print("Network update checks: %s" % ("allowed" if args.allow_network else "disabled"))
    return 0


def changelog_entries(source, old_version, new_version):
    path = Path(source) / "CHANGELOG.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for version, date, body in CHANGELOG_RE.findall(text):
        if semver(old_version) < semver(version) <= semver(new_version):
            lines = [line.strip(" -") for line in body.splitlines() if line.strip() and not line.startswith("###")]
            out.append({"version": version, "date": date, "summary": " ".join(lines)[:800]})
    out.sort(key=lambda item: semver(item["version"]), reverse=True)
    return out


def current_hash(path):
    return sha256_file(path) if Path(path).is_file() else None


def plan_changes(vault, old_manifest, source, source_components):
    old_by_path = {item["path"]: item for item in old_manifest.get("components", [])}
    new_by_path = {item["target"]: item for item in source_components}
    changes = []
    all_paths = sorted(set(old_by_path) | set(new_by_path))
    for rel in all_paths:
        dest, _ = resolved_child(vault, rel, "component target")
        actual = current_hash(dest)
        old = old_by_path.get(rel)
        new = new_by_path.get(rel)
        action, conflict, reason = "noop", False, "unchanged"
        if old and new and old.get("ownership") != new["ownership"]:
            action, conflict, reason = "conflict", True, "ownership changed"
        elif new and new["ownership"] == "user":
            if old:
                action = "preserve-user" if actual is not None else "preserve-user-missing"
                if new["source_hash"] != old.get("base_hash"):
                    reason = "upstream template changed; user-owned file preserved for manual review"
                else:
                    reason = "user-owned components are never overwritten or recreated"
            elif actual is None:
                action, reason = "add-user", "new user-owned component"
            else:
                action, reason = "adopt-user", "existing file becomes user-owned without overwrite"
        elif old and not new:
            if old.get("ownership") == "user":
                action, reason = "preserve-retired-user", "removed upstream, retained because user-owned"
            elif actual is None:
                action, reason = "remove-missing", "already absent"
            elif actual == old.get("base_hash"):
                action, reason = "remove", "upstream-owned component retired"
            else:
                action, conflict, reason = "conflict", True, "retired upstream file has local changes"
        elif new and not old:
            if actual is None:
                action, reason = "add", "new upstream-owned component"
            elif actual == new["source_hash"]:
                action, reason = "adopt", "existing bytes already match upstream"
            else:
                action, conflict, reason = "conflict", True, "new upstream target already exists"
        elif old and new:
            if actual is None:
                action, conflict, reason = "conflict", True, "upstream-owned component is missing"
            elif actual != old.get("base_hash"):
                if new["source_hash"] == old.get("base_hash"):
                    action, conflict, reason = "conflict", True, "upstream-owned component has local changes"
                else:
                    action, conflict, reason = "conflict", True, "upstream and local bytes both changed"
            elif new["source_hash"] == old.get("base_hash"):
                action, reason = "noop", "upstream and local bytes unchanged"
            elif actual == old.get("base_hash"):
                action, reason = "update", "upstream changed and local bytes still match installed base"
        changes.append(
            {
                "path": rel,
                "action": action,
                "ownership": (new or old).get("ownership"),
                "reason": reason,
                "conflict": conflict,
                "current_hash": actual,
                "old_base_hash": old.get("base_hash") if old else None,
                "source": new.get("source") if new else None,
                "source_hash": new.get("source_hash") if new else None,
            }
        )
    return changes


def create_plan(args):
    vault = Path(args.vault).resolve()
    old = load_manifest(vault)
    source, spec, source_components, commit = validate_source(args.source)
    old_version = old["installed"]["version"]
    if semver(spec["version"]) <= semver(old_version) and not args.allow_same_version:
        raise HarnessError("source v%s is not newer than installed v%s" % (spec["version"], old_version))
    changes = plan_changes(vault, old, source, source_components)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "product": "harness-kb-upgrade-plan",
        "created_at": utc_now(),
        "vault": str(vault),
        "from": {"version": old_version, "commit": old["installed"]["commit"]},
        "to": {"version": spec["version"], "commit": commit},
        "source": str(source),
        "source_spec_hash": sha256_file(source / SOURCE_SPEC),
        "installed_manifest_hash": sha256_file(manifest_path(vault)),
        "changes": changes,
        "gates": spec["gates"],
        "migration_impact": spec.get("migration_impact") or [],
        "release_notes": changelog_entries(source, old_version, spec["version"]),
    }
    plan["plan_id"] = uuid.uuid4().hex[:16]
    output = Path(args.out).resolve() if args.out else vault / PLAN_DIR_REL / (
        "v%s-to-v%s-%s.json" % (old_version, spec["version"], plan["plan_id"])
    )
    if not is_within(output, vault):
        raise HarnessError("plan output must stay inside the target vault")
    if output.exists():
        raise HarnessError("refusing to overwrite an existing reviewed plan: %s" % output)
    write_json(output, plan)
    conflicts = [item for item in changes if item["conflict"]]
    writes = [item for item in changes if item["action"] in ("add", "add-user", "update", "remove")]
    print("PLAN %s: v%s -> v%s" % (plan["plan_id"], old_version, spec["version"]))
    print("Changes: %d write/remove, %d preserved, %d conflict" % (
        len(writes),
        sum(1 for item in changes if item["action"].startswith("preserve") or item["action"].startswith("adopt")),
        len(conflicts),
    ))
    for item in changes:
        if item["action"] != "noop":
            print("  %-22s %s - %s" % (item["action"].upper(), item["path"], item["reason"]))
    if plan["migration_impact"]:
        print("Migration impact:")
        for line in plan["migration_impact"]:
            print("  - %s" % line)
    if plan["release_notes"]:
        print("Releases covered: %s" % ", ".join("v" + item["version"] for item in plan["release_notes"]))
    print("Plan written: %s" % output)
    if conflicts:
        print("Resolve conflicts before creating a fresh plan; this plan cannot be applied.")
        return 2
    print("Apply only after review: `python .harness/harness.py apply . --plan \"%s\"`" % output)
    return 0


def create_backup(vault, plan, paths):
    root = Path(vault) / BACKUP_DIR_REL / plan["plan_id"]
    if root.exists():
        raise HarnessError("backup already exists for plan %s" % plan["plan_id"])
    root.mkdir(parents=True)
    records = []
    for rel in sorted(set(paths)):
        src, safe_rel = resolved_child(vault, rel, "backup path")
        record = {"path": safe_rel.as_posix(), "existed": src.is_file(), "sha256": None, "backup": None}
        if src.is_file():
            data = src.read_bytes()
            backup_rel = Path("files") / safe_rel
            atomic_write(root / backup_rel, data)
            record.update({"sha256": sha256_bytes(data), "backup": backup_rel.as_posix()})
        records.append(record)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "created_at": utc_now(),
        "from": plan["from"],
        "to": plan["to"],
        "records": records,
    }
    write_json(root / "backup.json", metadata)
    return root, metadata


def restore_backup(vault, backup_root, metadata):
    vault = Path(vault).resolve()
    for record in reversed(metadata.get("records", [])):
        dest, _ = resolved_child(vault, record["path"], "restore path")
        if record["existed"]:
            src, _ = resolved_child(backup_root, record["backup"], "backup file")
            data = src.read_bytes()
            if sha256_bytes(data) != record["sha256"]:
                raise HarnessError("backup hash mismatch: %s" % record["path"])
            atomic_write(dest, data)
        elif dest.is_file():
            dest.unlink()


def verify_plan_inputs(vault, plan, source, source_components, old_manifest):
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("product") != "harness-kb-upgrade-plan":
        raise HarnessError("unsupported or foreign plan")
    if Path(plan.get("vault", "")).resolve() != Path(vault).resolve():
        raise HarnessError("plan belongs to a different vault")
    if any(item.get("conflict") for item in plan.get("changes", [])):
        raise HarnessError("plan contains conflicts and cannot be applied")
    if sha256_file(manifest_path(vault)) != plan.get("installed_manifest_hash"):
        raise HarnessError("installed manifest changed after planning; create a fresh plan")
    if sha256_file(Path(source) / SOURCE_SPEC) != plan.get("source_spec_hash"):
        raise HarnessError("source release specification changed after planning")
    expected_changes = plan_changes(vault, old_manifest, source, source_components)
    if canonical_json(expected_changes) != canonical_json(plan.get("changes", [])):
        raise HarnessError("plan decisions no longer match source + target; create a fresh plan")
    for item in plan.get("changes", []):
        dest, _ = resolved_child(vault, item["path"], "component target")
        if current_hash(dest) != item.get("current_hash"):
            raise HarnessError("component changed after planning: %s" % item["path"])
        if item.get("source"):
            src, _ = resolved_child(source, item["source"], "component source")
            if current_hash(src) != item.get("source_hash"):
                raise HarnessError("source component changed after planning: %s" % item["source"])


def apply_plan(args):
    vault = Path(args.vault).resolve()
    plan_path = Path(args.plan).resolve()
    if not is_within(plan_path, vault):
        raise HarnessError("plan must be stored inside the target vault")
    plan = load_json(plan_path, "upgrade plan")
    source = Path(plan.get("source", "")).resolve()
    source_root, spec, source_components, commit = validate_source(source)
    if commit != plan.get("to", {}).get("commit") or spec["version"] != plan.get("to", {}).get("version"):
        raise HarnessError("source version/commit no longer matches the reviewed plan")
    old = load_manifest(vault)
    verify_plan_inputs(vault, plan, source_root, source_components, old)
    write_actions = {"add", "add-user", "update", "remove"}
    touched = [item["path"] for item in plan["changes"] if item["action"] in write_actions]
    touched.append(MANIFEST_REL.as_posix())
    backup_root, backup = create_backup(vault, plan, touched)
    try:
        for item in plan["changes"]:
            action = item["action"]
            dest, _ = resolved_child(vault, item["path"], "component target")
            if action in ("add", "add-user", "update"):
                src, _ = resolved_child(source_root, item["source"], "component source")
                atomic_write(dest, src.read_bytes())
            elif action == "remove" and dest.is_file():
                dest.unlink()

        new_records = []
        for component in source_components:
            dest, _ = resolved_child(vault, component["target"], "component target")
            if not dest.is_file():
                raise HarnessError("component missing after apply: %s" % component["target"])
            new_records.append(component_record(component, sha256_file(dest)))
        preserved = []
        for item in plan["changes"]:
            if item["action"] == "preserve-retired-user":
                preserved.append(
                    {"path": item["path"], "ownership": "user", "reason": item["reason"], "hash": item["current_hash"]}
                )
        consent = bool((old.get("update_checks") or {}).get("network_consent"))
        new_manifest = installed_manifest(
            spec,
            new_records,
            commit,
            consent,
            vault_id=old.get("vault_id"),
            preserved=preserved,
        )
        write_json(manifest_path(vault), new_manifest)
        failures = run_gates(vault, new_manifest["gates"])
        if failures:
            restore_backup(vault, backup_root, backup)
            restored = load_manifest(vault)
            restored_failures = run_gates(vault, restored["gates"])
            if restored_failures:
                print("UPGRADE ROLLED BACK, but %d restored gate(s) are red" % len(restored_failures))
            else:
                print("UPGRADE ROLLED BACK: restored gates are green")
            print("Failed new gates: %d; backup kept at %s" % (len(failures), backup_root))
            return 1
    except Exception:
        restore_backup(vault, backup_root, backup)
        raise
    print("UPGRADED Harness KB v%s -> v%s" % (plan["from"]["version"], plan["to"]["version"]))
    print("Backup: %s" % backup_root)
    print("Rollback handle: `python .harness/harness.py rollback . --backup %s`" % plan["plan_id"])
    return 0


def rollback(args):
    vault = Path(args.vault).resolve()
    backup_root, _ = resolved_child(vault, BACKUP_DIR_REL / args.backup, "backup directory")
    metadata = load_json(backup_root / "backup.json", "backup metadata")
    if metadata.get("plan_id") != args.backup:
        raise HarnessError("backup id does not match backup metadata")
    restore_backup(vault, backup_root, metadata)
    restored = load_manifest(vault)
    failures = run_gates(vault, restored["gates"])
    if failures:
        print("ROLLBACK RESTORED FILES, but %d restored gate(s) are red" % len(failures))
        return 1
    print("ROLLED BACK to Harness KB v%s using %s" % (restored["installed"]["version"], args.backup))
    return 0


def verify_install(args):
    vault = Path(args.vault).resolve()
    manifest = load_manifest(vault)
    problems = []
    for item in manifest.get("components", []):
        path, _ = resolved_child(vault, item["path"], "component target")
        actual = current_hash(path)
        if actual is None:
            problems.append("missing %s component: %s" % (item["ownership"], item["path"]))
        elif item["ownership"] == "upstream" and actual != item.get("installed_hash"):
            problems.append("upstream-owned component changed locally: %s" % item["path"])
    for problem in problems:
        print("MANIFEST FAIL: %s" % problem)
    failures = run_gates(vault, manifest["gates"])
    if problems or failures:
        return 1
    print("HARNESS VERIFIED: v%s, %d components, all gates green" % (
        manifest["installed"]["version"], len(manifest.get("components", []))
    ))
    return 0


def parser():
    ap = argparse.ArgumentParser(description="Scaffold and safely upgrade a Harness KB vault.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create an agent-ready harness in a clean target folder")
    p.add_argument("vault")
    p.add_argument("--source", help="Harness KB source checkout (auto-detected when run from the repo)")
    p.add_argument("--adopt", action="store_true", help="allow a reviewed non-empty target; still never overwrite collisions")
    consent = p.add_mutually_exclusive_group(required=True)
    consent.add_argument("--allow-network-checks", action="store_true", help="record consent for cached GitHub update checks")
    consent.add_argument("--no-network-checks", action="store_true", help="disable all network update checks")
    p.set_defaults(func=init_vault)

    p = sub.add_parser("check", help="use consent + cache to report how many stable releases are newer")
    p.add_argument("vault")
    p.add_argument("--cache-seconds", type=int)
    p.add_argument("--api-base", default="https://api.github.com")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=update_status)

    p = sub.add_parser("consent", help="change update-check network consent")
    p.add_argument("vault")
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--allow-network", action="store_true")
    choice.add_argument("--deny-network", action="store_true")
    p.set_defaults(func=set_consent)

    p = sub.add_parser("plan", help="write a reviewable upgrade plan without changing installed components")
    p.add_argument("vault")
    p.add_argument("--source", required=True, help="newer Harness KB checkout")
    p.add_argument("--out", help="plan path inside the vault")
    p.add_argument("--allow-same-version", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=create_plan)

    p = sub.add_parser("apply", help="apply one reviewed plan with backup, gates and automatic rollback")
    p.add_argument("vault")
    p.add_argument("--plan", required=True)
    p.set_defaults(func=apply_plan)

    p = sub.add_parser("rollback", help="restore every path captured by one upgrade backup")
    p.add_argument("vault")
    p.add_argument("--backup", required=True)
    p.set_defaults(func=rollback)

    p = sub.add_parser("verify", help="verify manifest ownership/hashes and run every installed gate")
    p.add_argument("vault")
    p.set_defaults(func=verify_install)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except HarnessError as exc:
        print("HARNESS ERROR: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("HARNESS ERROR: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
