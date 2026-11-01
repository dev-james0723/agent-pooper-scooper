"""Collection within the closed APS producer protocol, never arbitrary-root cleanup.

Only the managed authority may supply an execution fence. Filesystem observations
are not atomic identity-and-delete or enforcement against out-of-protocol writers.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import stat
import time

from . import disk, executor_platform, planner, schema

MAX_BATCH = 64
MAX_ENTRIES = 1024
MAX_PLAN_BYTES = 262_144
MAX_PLAN_AGE = 300
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REGISTERED_IDENTITY = ("dev", "ino", "uid", "size", "mtime_ns", "ctime_ns")


class ExecutionRefused(ValueError):
    """The supported managed-store contract could not be established."""


def _leaf(name):
    if (not isinstance(name, str) or not _NAME.fullmatch(name)
            or name.casefold() in {"head", "objects", "refs"}):
        raise ExecutionRefused("invalid managed object name")
    return name


def _fingerprint(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid,
            "mode": st.st_mode, "nlink": st.st_nlink, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "allocated_bytes": getattr(st, "st_blocks", 0) * 512}


def _regular(info):
    if (not stat.S_ISREG(info["mode"]) or info["nlink"] != 1
            or info["uid"] != os.getuid() or stat.S_IMODE(info["mode"]) != 0o600):
        raise ExecutionRefused("managed object type, owner, mode or link count changed")


@contextmanager
def _opened_object(root_fd, artifact):
    name = _leaf(artifact["name"])
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=root_fd)
    except OSError:
        raise ExecutionRefused("managed object could not be opened safely") from None
    try:
        info = _fingerprint(os.fstat(fd))
        _regular(info)
        named = _fingerprint(os.stat(name, dir_fd=root_fd, follow_symlinks=False))
        if info != named or any(info[key] != artifact[key] for key in _REGISTERED_IDENTITY):
            raise ExecutionRefused("managed object identity or content metadata changed")
        yield fd, info
    except OSError:
        raise ExecutionRefused("managed object observation failed") from None
    finally:
        os.close(fd)


def _inspect_flat_root(root_fd, root_path, expected, names, *, limit=MAX_ENTRIES):
    """Bounded observation of a newly created flat registry-owned directory."""
    if type(limit) is not int or not 0 < limit <= MAX_ENTRIES or len(names) > MAX_ENTRIES:
        raise ExecutionRefused("managed directory bound exceeded")
    for name in names:
        _leaf(name)
    try:
        root = _fingerprint(os.fstat(root_fd))
        if (not stat.S_ISDIR(root["mode"]) or stat.S_IMODE(root["mode"]) != 0o700
                or root["uid"] != os.getuid()
                or any(root[key] != expected[key] for key in ("dev", "ino", "uid"))):
            raise ExecutionRefused("managed root binding or permissions changed")
        parent, leaf = disk._open_parent(root_path)
        try:
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (root["dev"], root["ino"]):
            raise ExecutionRefused("managed root pathname no longer binds the selected directory")
        reasons, _ = planner._root_scope(root_path, root)
        if reasons:
            raise ExecutionRefused("managed root scope is broad or unverified")
        if planner._ancestor_git(root_fd):
            raise ExecutionRefused("managed root intersects Git or unverified ancestry")
        observed = {}
        with os.scandir(root_fd) as entries:
            for number, entry in enumerate(entries, 1):
                if number > limit:
                    raise ExecutionRefused("managed directory entry bound exceeded")
                if entry.name not in names:
                    raise ExecutionRefused("unregistered entry in managed directory")
                _leaf(entry.name)
                info = _fingerprint(entry.stat(follow_symlinks=False))
                _regular(info)
                if info["dev"] != root["dev"]:
                    raise ExecutionRefused("managed object crosses a device boundary")
                observed[entry.name] = info
        return observed
    except OSError:
        raise ExecutionRefused("managed directory observation failed") from None


class _NullOr:
    def __init__(self, spec):
        self.spec = spec

    def check(self, value, where):
        return [] if value is None else self.spec.check(value, where)


_N = schema._NonNegativeInt()
_ID = schema._Pattern(r"^[A-Za-z0-9_.:-]{1,128}$", "managed identifier")
_HEX = schema._Pattern(r"^[0-9a-f]{64}$", "SHA-256 digest")
_IDENTITY = schema._Object({key: _N for key in _REGISTERED_IDENTITY})
_ROOT = schema._Object({"dev": _N, "ino": _N, "uid": _N, "path": schema._AbsolutePath()})
_ARTIFACT = schema._Object({
    "artifact_id": _ID, "name": _ID, "task_id": _ID, "origin_store_id": _ID,
    "class": schema._Enum(schema.ARTIFACT_CLASSES), "disposable": schema._Bool(),
    "origin_generation": _N, "created_at": schema._Timestamp(), "last_write_at": schema._Timestamp(),
    "retired": schema._Bool(), "pending": schema._Bool(), "allocated_bytes": _N,
    **{key: _N for key in _REGISTERED_IDENTITY},
})
_CLAIM = schema._Object({"claim_id": _ID, "kind": schema._Enum(("reference", "lease")),
                         "artifact_id": _ID, "task_id": _ID, "producer_generation": _N,
                         "expires_at": _NullOr(schema._Timestamp())})
_SNAPSHOT = schema._Object({
    "store_id": _ID, "broker_epoch": _ID, "revision": _N, "task_id": _ID, "producer_generation": _N,
    "task_status": schema._Enum(("active", "paused", "resumable", "completed", "failed")),
    "terminal_at": _NullOr(schema._Timestamp()), "grace_seconds": _N, "root": _ROOT,
    "artifacts": schema._Array(_ARTIFACT, MAX_ENTRIES), "inventory": schema._Array(_ARTIFACT, MAX_ENTRIES),
    "registered_names": schema._Array(_ID, MAX_ENTRIES), "references": schema._Array(_CLAIM, MAX_ENTRIES),
    "leases": schema._Array(_CLAIM, MAX_ENTRIES), "recovery_required": schema._Bool(),
})
_REASONS = ("protected_or_not_disposable", "retired", "task_not_terminal", "clock_unverified",
            "grace_period", "reference_present", "lease_present")
_DECISION = schema._Object({"artifact_id": _ID, "decision": schema._Enum(("KEEP", "WOULD_REMOVE")),
                            "reasons": schema._Array(schema._Enum(_REASONS), len(_REASONS)),
                            "identity": _IDENTITY, "apparent_bytes": _N, "allocated_bytes": _N})
_PLAN = schema._Object({
    "schema": schema._Const("agent-pooper-scooper/managed-plan"), "schema_version": schema._Const(1),
    "store_id": _ID, "broker_epoch": _ID, "task_id": _ID, "revision": _N, "producer_generation": _N,
    "planned_at": schema._Timestamp(), "expires_at": schema._Timestamp(), "dry_run": schema._Const(True),
    "snapshot_digest": _HEX, "filesystem_digest": _HEX,
    "decisions": schema._Array(_DECISION, MAX_BATCH),
    "candidate": schema._Object({"artifact_ids": schema._Array(_ID, MAX_BATCH), "count": _N,
                                  "apparent_bytes": _N, "allocated_bytes": _N}),
    "execution": schema._Object({"status": schema._Enum(("OWNER_APPROVAL_REQUIRED", "KEEP"))}),
    "removed": schema._Object({"count": schema._Const(0), "bytes": schema._Const(0)}),
    "reclaimed": schema._Object({"bytes": schema._Const(None)}), "plan_digest": _HEX,
})


def _digest(value):
    return planner.digest_document(value)


def _deadline(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 30:
        raise ExecutionRefused("invalid bounded collection deadline")
    return time.monotonic() + seconds


def _owner(value):
    from .authority import ManagedAuthority
    if type(value) is not ManagedAuthority:
        raise ExecutionRefused("collection requires the managed owner authority, not an evidence snapshot")
    return value


def _validate_plan(plan, *, now=None):
    if len(json.dumps(plan, ensure_ascii=False).encode()) > MAX_PLAN_BYTES or _PLAN.check(plan, "plan"):
        raise ExecutionRefused("managed plan schema refused")
    if plan["plan_digest"] != _digest({k: v for k, v in plan.items() if k != "plan_digest"}):
        raise ExecutionRefused("managed plan digest changed")
    ids = [d["artifact_id"] for d in plan["decisions"]]
    candidates = [d for d in plan["decisions"] if d["decision"] == "WOULD_REMOVE"]
    if (len(set(ids)) != len(ids) or any(bool(d["reasons"]) != (d["decision"] == "KEEP") for d in plan["decisions"])
            or plan["candidate"] != {"artifact_ids": [d["artifact_id"] for d in candidates], "count": len(candidates),
                                     "apparent_bytes": sum(d["apparent_bytes"] for d in candidates),
                                     "allocated_bytes": sum(d["allocated_bytes"] for d in candidates)}):
        raise ExecutionRefused("managed plan decisions or accounting changed")
    start, end = schema.parse_timestamp(plan["planned_at"]), schema.parse_timestamp(plan["expires_at"])
    if not 0 < (end - start).total_seconds() <= MAX_PLAN_AGE or (now is not None and not start <= now < end):
        raise ExecutionRefused("managed plan is stale or has an invalid clock")
    return plan


def load_plan_text(text, *, public=False):
    try:
        if not isinstance(text, str) or len(text.encode()) > MAX_PLAN_BYTES:
            raise ExecutionRefused("managed plan exceeds bound")
        return _validate_plan(schema.parse_json(text))
    except Exception:
        pass
    del text
    raise ExecutionRefused("managed plan input refused") from None


def plan_schema():
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **_PLAN.schema()}


def _snapshot(value):
    if _SNAPSHOT.check(value, "snapshot") or value["recovery_required"] or not 1 <= value["grace_seconds"] <= 31_536_000:
        raise ExecutionRefused("managed authority state is invalid or requires recovery")
    inventory = value["inventory"]
    ids = [item["artifact_id"] for item in inventory]
    names = [item["name"] for item in inventory]
    identities = [(item["dev"], item["ino"]) for item in inventory]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names) or len(set(identities)) != len(identities):
        raise ExecutionRefused("managed registrations repeat an object identity")
    if sorted(names) != value["registered_names"]:
        raise ExecutionRefused("managed registry inventory is incomplete")
    selected = {item["artifact_id"]: item for item in inventory if item["task_id"] == value["task_id"]}
    if _digest(sorted(selected.values(), key=lambda a: a["artifact_id"])) != _digest(sorted(value["artifacts"], key=lambda a: a["artifact_id"])):
        raise ExecutionRefused("managed task inventory differs from registry")
    for item in inventory:
        _leaf(item["name"])
        if item["origin_store_id"] != value["store_id"] or item["pending"]:
            raise ExecutionRefused("object origin or retirement state is unverified")
    for key, kind in (("references", "reference"), ("leases", "lease")):
        if any(c["kind"] != kind or c["artifact_id"] not in selected for c in value[key]):
            raise ExecutionRefused("managed claims are unverified")
    return value


def _make_plan(fence, *, artifact_ids=None, now=None):
    fence.check()
    state = _snapshot(fence.snapshot())
    now = now or datetime.now(timezone.utc)
    inventory = {item["name"]: item for item in state["inventory"]}
    actual = _inspect_flat_root(fence.root_fd, state["root"]["path"], state["root"], set(inventory))
    for name, item in inventory.items():
        if name not in actual:
            if not item["retired"]:
                raise ExecutionRefused("registered live object is missing")
        elif any(actual[name][key] != item[key] for key in _REGISTERED_IDENTITY):
            raise ExecutionRefused("registered object metadata changed")
    native = executor_platform.observe(fence.root_fd, state["root"]["path"])
    selected = {item["artifact_id"]: item for item in state["artifacts"]}
    ids = sorted(selected) if artifact_ids is None else list(artifact_ids)
    if len(ids) > MAX_BATCH or len(set(ids)) != len(ids) or any(ident not in selected for ident in ids):
        raise ExecutionRefused("select at most 64 distinct task-owned object IDs")
    terminal = schema.parse_timestamp(state["terminal_at"])
    decisions = []
    for ident in sorted(ids):
        item = selected[ident]
        observed = actual.get(item["name"])
        reasons = []
        if item["class"] in schema.PROTECTED_CLASSES or not item["disposable"]:
            reasons.append("protected_or_not_disposable")
        if item["retired"]:
            reasons.append("retired")
        if terminal is None or state["task_status"] not in ("completed", "failed"):
            reasons.append("task_not_terminal")
        else:
            clocks = [terminal, schema.parse_timestamp(item["created_at"]), schema.parse_timestamp(item["last_write_at"])]
            if observed:
                clocks += [datetime.fromtimestamp(observed[key] / 1e9, timezone.utc) for key in ("mtime_ns", "ctime_ns")]
            if max(clocks) > now:
                reasons.append("clock_unverified")
            elif (now - max(clocks)).total_seconds() < state["grace_seconds"]:
                reasons.append("grace_period")
        if any(c["artifact_id"] == ident for c in state["references"]):
            reasons.append("reference_present")
        if any(c["artifact_id"] == ident for c in state["leases"]):
            reasons.append("lease_present")
        decisions.append({"artifact_id": ident, "decision": "KEEP" if reasons else "WOULD_REMOVE",
                          "reasons": sorted(set(reasons)), "identity": {key: item[key] for key in _REGISTERED_IDENTITY},
                          "apparent_bytes": observed["size"] if observed else 0,
                          "allocated_bytes": observed["allocated_bytes"] if observed else 0})
    candidates = [d for d in decisions if d["decision"] == "WOULD_REMOVE"]
    data = {"schema": "agent-pooper-scooper/managed-plan", "schema_version": 1,
            **{key: state[key] for key in ("store_id", "broker_epoch", "task_id", "revision", "producer_generation")},
            "planned_at": schema.format_timestamp(now), "expires_at": schema.format_timestamp(now + timedelta(seconds=MAX_PLAN_AGE)),
            "dry_run": True, "snapshot_digest": _digest(state),
            "filesystem_digest": _digest({"root": _fingerprint(os.fstat(fence.root_fd)), "objects": actual, "platform": native}),
            "decisions": decisions,
            "candidate": {"artifact_ids": [d["artifact_id"] for d in candidates], "count": len(candidates),
                          "apparent_bytes": sum(d["apparent_bytes"] for d in candidates),
                          "allocated_bytes": sum(d["allocated_bytes"] for d in candidates)},
            "execution": {"status": "OWNER_APPROVAL_REQUIRED" if candidates else "KEEP"},
            "removed": {"count": 0, "bytes": 0}, "reclaimed": {"bytes": None}}
    data["plan_digest"] = _digest(data)
    fence.check()
    return _validate_plan(data), state, actual, native


def _display(data):
    if data.get("schema") == "agent-pooper-scooper/managed-plan":
        return {"schema": "agent-pooper-scooper/managed-preview", "dry_run": True,
                "candidate": {key: value for key, value in data["candidate"].items() if key != "artifact_ids"},
                "decisions": [{key: value for key, value in item.items() if key in {"decision", "reasons", "apparent_bytes", "allocated_bytes"}}
                              for item in data["decisions"]], "execution": data["execution"],
                "removed": data["removed"], "reclaimed": data["reclaimed"]}
    return {key: data[key] for key in ("schema", "status", "dry_run", "candidate", "removed", "reclaimed", "reconciliation_required")}


def create_plan(authority, task_id, *, artifact_ids=None, deadline_seconds=15, public=False):
    try:
        owner = _owner(authority)
        with owner.transaction(task_id, expected_revision=None, deadline=_deadline(deadline_seconds)) as fence:
            data, _, _, _ = _make_plan(fence, artifact_ids=artifact_ids)
        return _display(data) if public else data
    except Exception:
        if not public:
            raise
    del authority, task_id, artifact_ids
    raise ExecutionRefused("managed preview refused") from None


def _semantic(plan):
    return {key: value for key, value in plan.items() if key not in {"planned_at", "expires_at", "plan_digest"}}


def _fresh(fence, plan):
    _validate_plan(plan, now=datetime.now(timezone.utc))
    current, state, observed, native = _make_plan(fence, artifact_ids=[d["artifact_id"] for d in plan["decisions"]])
    if _digest(_semantic(current)) != _digest(_semantic(plan)):
        raise ExecutionRefused("reviewed managed plan is stale or altered")
    return state, observed, native


def approve_plan(authority, plan, operation_id, *, ttl_seconds=30, deadline_seconds=15):
    owner = _owner(authority)
    _validate_plan(plan, now=datetime.now(timezone.utc))
    if not plan["candidate"]["artifact_ids"]:
        raise ExecutionRefused("managed plan has no eligible objects")
    with owner.transaction(plan["task_id"], expected_revision=plan["revision"], deadline=_deadline(deadline_seconds)) as fence:
        _fresh(fence, plan)
    return owner.approve(plan["task_id"], plan["plan_digest"], plan["candidate"]["artifact_ids"],
                         plan["revision"], operation_id, ttl_seconds=ttl_seconds)


def _result(plan, status, *, dry_run, receipts=(), prior=(), pending=()):
    verified = [receipt for receipt in receipts if receipt.get("outcome") == "unlinked"]
    return {"schema": "agent-pooper-scooper/managed-collection", "schema_version": 1,
            "status": status, "dry_run": dry_run, "plan_digest": plan["plan_digest"],
            "store_id": plan["store_id"], "task_id": plan["task_id"],
            "candidate": {key: value for key, value in plan["candidate"].items() if key != "artifact_ids"},
            "removed": {"count": len(verified), "bytes": sum(item["removed_bytes"] for item in verified)},
            "reclaimed": {"bytes": None}, "reconciliation_required": bool(pending),
            "receipts": list(receipts), "prior_receipts": list(prior), "pending_artifact_ids": list(pending),
            "audit": {"status": "RECOVERY_REQUIRED" if pending else ("not_attempted" if dry_run else "durable_receipts"),
                      "receipt_count": len(receipts)}}


def _effect_receipt(plan, operation_id, artifact_id, identity, outcome, **extra):
    return {"store_id": plan["store_id"], "operation_id": operation_id, "artifact_id": artifact_id,
            "plan_digest": plan["plan_digest"], "identity": identity, "outcome": outcome,
            "removed_bytes": None, "allocated_bytes": None, "at": schema.format_timestamp(), **extra}


def _collect(authority, plan, *, execute, approval_id, operation_id, deadline_seconds):
    owner = _owner(authority)
    if type(execute) is not bool:
        raise ExecutionRefused("execute must be an explicit boolean")
    _validate_plan(plan)
    ids = plan["candidate"]["artifact_ids"]
    if execute:
        if not ids or _ID.check(approval_id, "approval") or _ID.check(operation_id, "operation"):
            raise ExecutionRefused("execution requires a reviewed nonempty plan, owner approval and operation ID")
        prior = owner.operation_receipts(operation_id, plan["plan_digest"], approval_id, ids)
        if prior is not None:
            return _result(plan, "IDEMPOTENT_REPLAY", dry_run=False, prior=prior)
    receipts = []
    with owner.transaction(plan["task_id"], expected_revision=plan["revision"], deadline=_deadline(deadline_seconds)) as fence:
        state, _, native = _fresh(fence, plan)
        if not execute:
            return _result(plan, "DRY_RUN", dry_run=True)
        records = {item["artifact_id"]: item for item in state["artifacts"]}
        names = set(state["registered_names"])
        for artifact_id in ids:
            item = records[artifact_id]
            intent = None
            identity = {key: item[key] for key in _REGISTERED_IDENTITY}
            try:
                with _opened_object(fence.root_fd, item) as (object_fd, info):
                    fence.check()
                    _inspect_flat_root(fence.root_fd, state["root"]["path"], state["root"], names)
                    observed_mount = executor_platform.observe(object_fd, native["canonical_root"])
                    if _digest(observed_mount) != _digest(native):
                        raise ExecutionRefused("managed object mount observation changed")
                    intent = fence.prepare(operation_id, plan["plan_digest"], artifact_id, identity, approval_id)
                    if isinstance(intent, dict):
                        return _result(plan, "IDEMPOTENT_REPLAY" if not receipts else "PARTIAL",
                                       dry_run=False, receipts=receipts, prior=[intent])
                    # prepare has durably retired/revoked the object while the
                    # SAME producer/ref/resume mutex+flock remains held.
                    fence.check(intent)
                    named = _fingerprint(os.stat(item["name"], dir_fd=fence.root_fd, follow_symlinks=False))
                    if named != info:
                        raise ExecutionRefused("managed object changed after durable intent")
                    # Metadata/SQLite observations can outlast the earlier
                    # check. Revalidate after them and sample the clock again
                    # after check's own I/O, immediately before dispatch.
                    fence.check(intent)
                    if time.monotonic() >= fence.deadline:
                        raise ExecutionRefused("managed effect deadline expired")
                    os.unlink(item["name"], dir_fd=fence.root_fd)
                    try:
                        os.stat(item["name"], dir_fd=fence.root_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ExecutionRefused("managed namespace absence is unverified")
                    post = os.fstat(object_fd)
                    if (post.st_dev, post.st_ino, post.st_uid, post.st_size, post.st_nlink) != (
                            info["dev"], info["ino"], info["uid"], info["size"], 0):
                        raise ExecutionRefused("unlinked object identity or link count is unverified")
                    executor_platform.sync_directory(fence.root_fd)
                    if _digest(executor_platform.observe(fence.root_fd, native["canonical_root"])) != _digest(native):
                        raise ExecutionRefused("managed post-unlink mount observation changed")
                    receipt = _effect_receipt(plan, operation_id, artifact_id, identity, "unlinked",
                                              namespace_absent=True,
                                              post_unlink={"dev": post.st_dev, "ino": post.st_ino, "nlink": 0})
                    stored = fence.finish(intent, "unlinked", receipt)
                    if (not isinstance(stored, dict) or stored.get("outcome") != "unlinked"
                            or stored.get("identity") != identity or type(stored.get("removed_bytes")) is not int
                            or stored["removed_bytes"] != info["size"]):
                        return _result(plan, "RECOVERY_REQUIRED", dry_run=False, receipts=receipts, pending=[artifact_id])
                    receipts.append(stored)
            except Exception:
                if intent is not None and not isinstance(intent, dict):
                    # Never infer successful removal from ENOENT, a raised
                    # syscall, elapsed time, or a failed durable finish.
                    try:
                        fence.finish(intent, "unknown", _effect_receipt(
                            plan, operation_id, artifact_id, identity, "unknown"))
                    except Exception:
                        pass  # Durable prepared intent still blocks recovery.
                    return _result(plan, "RECOVERY_REQUIRED", dry_run=False, receipts=receipts, pending=[artifact_id])
                if receipts:
                    return _result(plan, "PARTIAL", dry_run=False, receipts=receipts)
                raise
    return _result(plan, "COMPLETED", dry_run=False, receipts=receipts)


def collect(authority, plan, *, execute=False, approval_id=None, operation_id=None,
            deadline_seconds=15, public=False):
    """Collect only approved broker-owned objects under the live shared fence.

    Default is dry-run. The managed protocol excludes unmediated filesystem or
    mount actors; stat plus unlink is intentionally not called inode-conditional.
    """
    try:
        data = _collect(authority, plan, execute=execute, approval_id=approval_id,
                        operation_id=operation_id, deadline_seconds=deadline_seconds)
        return _display(data) if public else data
    except Exception:
        if not public:
            raise
    del authority, plan, approval_id, operation_id
    raise ExecutionRefused("managed collection refused") from None
