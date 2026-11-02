"""Conservative regular-file planner. Execution is unconditionally blocked."""
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat

from . import adapters, disk, events, schema

MAX_SNAPSHOT_AGE_SECONDS = 300
ROOT_SCAN_ENTRIES = 10_000
BROAD_ROOT_PATHS = ("/", "/Users", "/home", "/tmp", "/private/tmp", "/var",
                    "/private/var", "/var/tmp", "/private/var/tmp")
EXECUTION_BLOCKERS = (
    "atomic_identity_delete_unavailable", "complete_platform_mount_proof_unverified",
    "authoritative_live_reference_adapter_unavailable",
)


def digest_document(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _intersects(left, right):
    return _within(left, right) or _within(right, left)


def _git_directory(fd):
    """Inspect marker metadata only; never read or follow a gitfile."""
    try:
        try:
            os.stat(".git", dir_fd=fd, follow_symlinks=False)
            return "git_intersection"
        except FileNotFoundError:
            pass
        markers = []
        for name in ("HEAD", "objects", "refs"):
            try:
                markers.append(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
            except FileNotFoundError:
                return None
        if stat.S_ISREG(markers[0]) and all(stat.S_ISDIR(m) for m in markers[1:]):
            return "git_intersection"
    except OSError:
        return "git_inspection_unknown"
    return None


def _ancestor_git(fd, identities=None):
    current = os.dup(fd)
    try:
        for _ in range(disk.DEFAULT_MAX_DEPTH + 1):
            before = os.fstat(current)
            if identities is not None:
                identities.append((before.st_dev, before.st_ino))
            problem = _git_directory(current)
            if problem:
                return problem
            parent = os.open("..", disk._DIR_FLAGS, dir_fd=current)
            after = os.fstat(parent)
            os.close(current)
            current = parent
            if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
                return None
        return "git_inspection_unknown"
    finally:
        os.close(current)


def _metadata(path):
    try:
        parent, name = disk._open_parent(path)
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            ancestors = []
            problem = _ancestor_git(parent, ancestors)
        finally:
            os.close(parent)
    except OSError:
        return None, "path_unverified"
    return {"dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid,
            "mode": st.st_mode, "nlink": st.st_nlink, "size": st.st_size,
            "allocated": getattr(st, "st_blocks", 0) * 512,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "ancestors": ancestors}, problem


def _root_scope_identity(path):
    """Inspect a forbidden directory's identity without walking it or following links."""
    if not os.path.isabs(path):
        return {"status": "unknown"}
    try:
        parent, name = disk._open_parent(path)
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "not_directory" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "unknown"}
    if not stat.S_ISDIR(st.st_mode):
        return {"status": "not_directory"}
    return {"status": "directory", "dev": st.st_dev, "ino": st.st_ino}


def _root_scope(path, meta):
    home = os.path.expanduser("~")
    forbidden = set(BROAD_ROOT_PATHS) | {home}
    if path in forbidden:
        return ["root_scope_too_broad"], {"literal": path, "home": home}
    if meta is None or not stat.S_ISDIR(meta["mode"]):
        return ["root_scope_unverified"], {}
    observations, reasons = {}, []
    for forbidden_path in sorted(forbidden):
        observed = _root_scope_identity(forbidden_path)
        observations[forbidden_path] = observed
        if observed["status"] == "directory":
            if (observed["dev"], observed["ino"]) == (meta["dev"], meta["ino"]):
                reasons.append("root_scope_too_broad")
        elif observed["status"] == "unknown" or forbidden_path == home:
            reasons.append("root_scope_unverified")
        # Absent/non-directory built-ins cannot be admitted physical directory
        # roots. Their physical macOS spellings are listed independently.
    return reasons, observations


def _root_state(root, approval):
    meta, problem = _metadata(root["path"])
    reasons = [problem] if problem else []
    scope_reasons, scope_observations = _root_scope(root["path"], meta)
    reasons.extend(scope_reasons)
    if meta is not None:
        meta["scope_observations"] = scope_observations
    if not approval["approved"]:
        reasons.append("root_not_approved")
    if (meta is None or not stat.S_ISDIR(meta["mode"])
            or (meta["dev"], meta["ino"]) != (root["dev"], root["ino"])
            or (root["dev"], root["ino"]) != (approval["dev"], approval["ino"])):
        reasons.append("root_identity_changed")
    if meta and (meta["uid"] != approval["uid"] or meta["uid"] != os.getuid()):
        reasons.append("owner_unverified")
    if meta and meta["mode"] & 0o022:
        reasons.append("root_permissions_unverified")
    usage = None
    if not reasons:
        usage = disk.walk_usage(root["path"], expect=(root["dev"], root["ino"]),
                                max_entries=ROOT_SCAN_ENTRIES, directory_check=_git_directory)
        if "git_intersection" in usage.unknowns_by_reason:
            reasons.append("git_intersection")
        elif not usage.complete:
            reasons.append("root_scan_incomplete")
        if usage.skipped_by_reason:
            reasons.append("root_contains_unverified_entries")
    return meta, sorted(set(reasons)), usage.as_dict() if usage else None


def build_plan(manifest, adapter=None, *, now=None, task_events=None, public=False):
    """Build a read-only plan; public failures expose only a generic error."""
    try:
        return _build_plan(manifest, adapter, now=now, task_events=task_events, public=public)
    except Exception:
        if not public:
            raise
    # Raise outside the handler so private validation exceptions are not retained.
    del manifest, adapter, task_events
    raise schema.ValidationError(["planning refused (details withheld in public mode)"]) from None


def _build_plan(manifest, adapter=None, *, now=None, task_events=None, public=False):
    errors = schema.validate_manifest(manifest)
    if errors:
        raise schema.ValidationError(errors)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("planning time must include timezone")
    adapter = adapter or adapters.NullEvidenceAdapter()
    evidence = adapter.snapshot()
    digest = digest_document(manifest)
    global_reasons = []
    approvals = {}
    if evidence is None:
        global_reasons.append("evidence_unavailable")
    else:
        # Validate custom adapters as strictly as file adapters.
        evidence = schema.load_planner_evidence_text(json.dumps(evidence))
        if (evidence["task_id"], evidence["host"]) != (manifest["task_id"], manifest["host"]):
            global_reasons.append("evidence_scope_mismatch")
        if evidence["manifest_digest"] != digest:
            global_reasons.append("manifest_evidence_mismatch")
        observed, expires, terminal = (schema.parse_timestamp(evidence[k])
                                       for k in ("observed_at", "expires_at", "terminal_at"))
        if (observed > now or expires <= now or terminal > observed
                or (now - observed).total_seconds() > MAX_SNAPSHOT_AGE_SECONDS
                or not 0 < (expires - observed).total_seconds() <= MAX_SNAPSHOT_AGE_SECONDS
                or schema.parse_timestamp(manifest["updated_at"]) > observed):
            global_reasons.append("clock_or_freshness_unverified")
        if evidence["task_status"] not in events.TERMINAL_STATUSES:
            global_reasons.append("task_not_terminal")
        for name in ("references", "leases", "mounts", "worktrees"):
            if not evidence[name + "_complete"]:
                global_reasons.append(name + "_unknown")
        approvals = {r["path"]: r for r in evidence["approved_roots"]}
    if task_events is not None:
        checked = events.parse_events_text("\n".join(json.dumps(e) for e in task_events), manifest["task_id"])
        if any(e["host"] != manifest["host"] for e in checked):
            global_reasons.append("event_scope_mismatch")
        status, _ = events.task_status(checked)
        latest = max(checked, key=lambda e: schema.parse_timestamp(e["occurred_at"]), default=None)
        if evidence and (status != evidence["task_status"] or latest is None
                         or schema.parse_timestamp(latest["occurred_at"])
                         != schema.parse_timestamp(evidence["terminal_at"])):
            global_reasons.append("event_evidence_mismatch")
        if status not in events.TERMINAL_STATUSES:
            global_reasons.append("task_not_terminal")

    roots = {}
    for root in manifest["roots"]:
        approval = approvals.get(root["path"])
        if approval:
            roots[root["path"]] = _root_state(root, approval)
            meta, reasons, usage = roots[root["path"]]
            if meta and (meta["mtime_ns"] > schema.parse_timestamp(evidence["observed_at"]).timestamp() * 1e9
                         or meta["ctime_ns"] > schema.parse_timestamp(evidence["observed_at"]).timestamp() * 1e9):
                reasons.append("root_changed_after_evidence")
    retained_paths = {artifact["path"] for artifact in manifest["artifacts"]
                      if artifact["class"] in schema.PROTECTED_CLASSES or not artifact["disposable"]}
    observations = {}

    def observe(path):
        if path not in observations:
            observations[path] = _metadata(path)
        return observations[path]

    def identity(meta):
        return meta["dev"], meta["ino"]

    def related(left, right, *, intersects=True):
        """True/False from observed objects; None means no safe alias proof."""
        left_meta, left_problem = observe(left)
        right_meta, right_problem = observe(right)
        # Literal overlap remains a reason to retain even if observation fails.
        if (_intersects(left, right) if intersects else _within(right, left)):
            return True
        if any(problem or meta is None or not (stat.S_ISREG(meta["mode"]) or stat.S_ISDIR(meta["mode"]))
               for meta, problem in ((left_meta, left_problem), (right_meta, right_problem))):
            return None
        return (identity(left_meta) == identity(right_meta)
                or identity(left_meta) in right_meta["ancestors"]
                or (intersects and identity(right_meta) in left_meta["ancestors"]))

    retained_identities = set()
    for artifact in manifest["artifacts"]:
        if artifact["path"] not in retained_paths:
            continue
        recorded = artifact["dev"], artifact["ino"]
        retained_identities.add(recorded)
        meta, problem = observe(artifact["path"])
        if (problem or meta is None or identity(meta) != recorded
                or not (stat.S_ISREG(meta["mode"]) or stat.S_ISDIR(meta["mode"]))):
            global_reasons.append("retained_identity_unverified")
    decisions, states = [], []
    for artifact in manifest["artifacts"]:
        reasons = list(global_reasons)
        meta, problem = observe(artifact["path"])
        if artifact["class"] in schema.PROTECTED_CLASSES or not artifact["disposable"]:
            reasons.append("protected_or_not_disposable")
        if (any(str(parent) in retained_paths for parent in PurePosixPath(artifact["path"]).parents)
                or (meta and (any(parent in retained_identities for parent in meta["ancestors"])
                              or (artifact["path"] not in retained_paths
                                  and identity(meta) in retained_identities)))):
            reasons.append("inside_retained_artifact")
        containing = [r for r in manifest["roots"] if artifact["path"] != r["path"]
                      and _within(artifact["path"], r["path"])]
        root = max(containing, key=lambda r: len(r["path"])) if containing else None
        approval = approvals.get(root["path"]) if root else None
        if root is None:
            reasons.append("outside_approved_root")
        elif approval is None:
            reasons.append("root_not_approved")
        else:
            reasons.extend(roots[root["path"]][1])
            if schema.parse_timestamp(approval["approved_at"]) > schema.parse_timestamp(evidence["observed_at"]):
                reasons.append("approval_clock_unverified")
            if (now - schema.parse_timestamp(evidence["terminal_at"])).total_seconds() < approval["grace_seconds"]:
                reasons.append("grace_period")
            checks = (
                (root["path"], evidence["protected_paths"], True,
                 "protected_path_intersection", "protected_path_identity_unverified"),
                (root["path"], evidence["mount_points"], False,
                 "mount_boundary_or_unknown", "mount_boundary_or_unknown"),
                (artifact["path"], [r["path"] for r in evidence["references"]], True,
                 "referenced", "reference_identity_unverified"),
                (artifact["path"], [lease["path"] for lease in evidence["leases"]], True,
                 "lease_present", "lease_identity_unverified"),
            )
            for pivot, paths, intersects, matched, unknown in checks:
                for path in paths:
                    result = related(pivot, path, intersects=intersects)
                    if result is not False:
                        reasons.append(matched if result is True else unknown)
        if problem:
            reasons.append(problem)
        if meta is None:
            reasons.append("artifact_unverified")
        else:
            if not stat.S_ISREG(meta["mode"]):
                reasons.append("artifact_not_regular")
            if (meta["dev"], meta["ino"]) != (artifact["dev"], artifact["ino"]):
                reasons.append("artifact_identity_changed")
            if approval and (meta["uid"] != approval["uid"] or meta["uid"] != os.getuid()):
                reasons.append("owner_unverified")
            if meta["nlink"] != 1:
                reasons.append("hardlinked")
            if root and meta["dev"] != root["dev"]:
                reasons.append("mount_boundary_or_unknown")
            if approval:
                try:
                    changes = [schema.parse_timestamp(evidence["terminal_at"]),
                               schema.parse_timestamp(manifest["updated_at"]),
                               schema.parse_timestamp(artifact["recorded_at"]),
                               datetime.fromtimestamp(meta["mtime_ns"] / 1e9, timezone.utc),
                               datetime.fromtimestamp(meta["ctime_ns"] / 1e9, timezone.utc)]
                    if max(changes) > schema.parse_timestamp(evidence["observed_at"]):
                        reasons.append("file_clock_unverified")
                    if (now - max(changes)).total_seconds() < approval["grace_seconds"]:
                        reasons.append("grace_period")
                except (OverflowError, OSError, ValueError):
                    reasons.append("file_clock_unverified")
        states.append({"path": artifact["path"], "metadata": meta})
        decisions.append({"path": artifact["path"], "decision": "KEEP" if reasons else "WOULD_REMOVE",
                          "reasons": sorted(set(reasons)),
                          "apparent_bytes": meta["size"] if meta and stat.S_ISREG(meta["mode"]) else None,
                          "allocated_bytes": meta["allocated"] if meta and stat.S_ISREG(meta["mode"]) else None})
    candidates = [d for d in decisions if d["decision"] == "WOULD_REMOVE"]
    evidence_digest = digest_document(evidence) if evidence else None
    state_digest = digest_document({"manifest": digest, "evidence": evidence_digest,
                                    "events": digest_document(task_events) if task_events is not None else None,
                                    "artifacts": states, "roots": roots, "decisions": decisions,
                                    "path_observations": observations})
    data = {"schema": "agent-pooper-scooper/plan", "schema_version": 1,
            "task_id": manifest["task_id"], "host": manifest["host"],
            "planned_at": schema.format_timestamp(now), "dry_run": True,
            "manifest_digest": digest, "state_digest": state_digest,
            "evidence": {"basis": adapter.basis, "revision": evidence["revision"] if evidence else None,
                         "digest": evidence_digest, "authenticated": False},
            "decisions": decisions,
            "candidate": {"count": len(candidates),
                          "apparent_bytes": sum(d["apparent_bytes"] for d in candidates),
                          "allocated_bytes": sum(d["allocated_bytes"] for d in candidates)},
            "removed": {"count": 0, "bytes": 0, "receipts": []},
            "reclaimed": {"bytes": None, "basis": "causality unverified; no executor"},
            "execution": {"status": "BLOCKED", "reasons": list(EXECUTION_BLOCKERS)},
            "audit": [{"at": schema.format_timestamp(now), "action": "plan.started",
                       "outcome": "dry_run", "manifest_digest": digest, "state_digest": state_digest}] +
                     [{"at": schema.format_timestamp(now), "action": "plan.artifact",
                       "path": d["path"], "outcome": d["decision"], "reasons": d["reasons"]}
                      for d in decisions] +
                     [{"at": schema.format_timestamp(now), "action": "plan.completed", "outcome": "execution_blocked"}]}
    data["plan_digest"] = digest_document(data)
    if public:
        from .report import redact_strict
        return redact_strict(data)
    return data


def revalidate_plan(plan, manifest, adapter, *, now=None, task_events=None):
    """Compare fresh evidence/state; a valid result still cannot enable execution."""
    try:
        if not isinstance(plan, dict) or plan.get("plan_digest") != digest_document(
                {k: v for k, v in plan.items() if k != "plan_digest"}):
            raise ValueError("invalid plan payload")
        moment = now or datetime.now(timezone.utc)
        planned_at = schema.parse_timestamp(plan["planned_at"])
        if (planned_at is None or planned_at > moment
                or (moment - planned_at).total_seconds() > MAX_SNAPSHOT_AGE_SECONDS
                or any(schema.parse_timestamp(entry["at"]) != planned_at for entry in plan["audit"])):
            raise ValueError("invalid or expired plan timestamp")
        current = build_plan(manifest, adapter, now=now, task_events=task_events)

        def stable(payload):
            normalized = {k: v for k, v in payload.items() if k not in ("planned_at", "plan_digest", "audit")}
            normalized["audit"] = [{k: v for k, v in entry.items() if k != "at"}
                                   for entry in payload["audit"]]
            return normalized

        valid = digest_document(stable(plan)) == digest_document(stable(current))
    except Exception:
        valid = False
    return {"valid": valid, "reason": "unchanged" if valid else "stale_or_unverifiable",
            "execution": "BLOCKED"}
