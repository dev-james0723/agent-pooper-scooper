"""Read-only task accounting from one trusted managed-store ledger view."""
import copy
import json

from . import schema
from .authority import ManagedAuthority

MAX_ARTIFACTS = 1024
MAX_FACTS = 2048
MAX_UNKNOWN = 4096
MAX_BYTES = 2 * 1024 * 1024


class ManagedReportError(ValueError):
    pass


class _Nullable:
    def __init__(self, spec):
        self.spec = spec

    def check(self, value, where):
        return [] if value is None else self.spec.check(value, where)


_N = schema._NonNegativeInt()
_ID = schema._Pattern(r"^[A-Za-z0-9_.:-]{1,128}$", "managed identifier")
_CODE = schema._Pattern(r"^[a-z][a-z0-9_.:-]{0,80}$", "safe accounting code")
_IDENTITY = schema._Object({key: _N for key in ("dev", "ino", "uid", "size", "mtime_ns", "ctime_ns")})
_ARTIFACT = schema._Object({
    "artifact_id": _ID, "class": schema._Enum(schema.ARTIFACT_CLASSES), "disposable": schema._Bool(),
    "retired": schema._Bool(), "pending": schema._Bool(), "origin_generation": _N,
    "created_at": schema._Timestamp(), "identity": _Nullable(_IDENTITY), "creation_verified": schema._Bool(),
    "created_bytes": _Nullable(_N), "created_allocated_bytes": _Nullable(_N),
    "current_status": schema._Enum(("present_verified", "missing", "changed", "unknown")),
    "current_bytes": _Nullable(_N), "current_allocated_bytes": _Nullable(_N),
    "reference_count": _N, "lease_count": _N,
})
_OUTCOME = schema._Object({"operation_id": _ID, "artifact_id": _ID,
    "outcome": schema._Enum(("pending", "unlinked", "not_removed", "unknown", "reconciled_missing", "reconciled_present")),
    "binding_verified": schema._Bool(), "removed_bytes": _Nullable(_N), "at": _Nullable(schema._Timestamp())})
_UNKNOWN = schema._Object({"artifact_id": _Nullable(_ID), "reason": _CODE})
_AUDIT = schema._Object({"event": _CODE, "artifact_id": _Nullable(_ID), "operation_id": _Nullable(_ID),
                        "status": _CODE, "at": _Nullable(schema._Timestamp())})
_VIEW = schema._Object({"schema": schema._Const("agent-pooper-scooper/managed-accounting"), "schema_version": schema._Const(1),
    "store_id": _ID, "task_id": _ID, "revision": _N,
    "task_status": schema._Enum(("active", "paused", "resumable", "completed", "failed", "unknown")),
    "observed_at": schema._Timestamp(), "recovery_required": schema._Bool(),
    "artifacts": schema._Array(_ARTIFACT, MAX_ARTIFACTS), "outcomes": schema._Array(_OUTCOME, MAX_FACTS),
    "unknowns": schema._Array(_UNKNOWN, MAX_UNKNOWN), "audit": schema._Array(_AUDIT, MAX_FACTS), "complete": schema._Bool()})


def _view(owner, task_id):
    if type(owner) is not ManagedAuthority or _ID.check(task_id, "task"):
        raise ManagedReportError("genuine managed owner and task required")
    value = owner.accounting_snapshot(task_id)
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES or _VIEW.check(value, "accounting"):
        raise ManagedReportError("fenced accounting schema refused")
    if value["task_id"] != task_id:
        raise ManagedReportError("accounting task binding changed")
    ids = [item["artifact_id"] for item in value["artifacts"]]
    if len(ids) != len(set(ids)):
        raise ManagedReportError("duplicate accounting artifact ID")
    for item in value["artifacts"]:
        if item["creation_verified"] and (item["identity"] is None or item["created_bytes"] != item["identity"]["size"]):
            raise ManagedReportError("invalid verified creation accounting")
        if item["current_status"] == "present_verified" and (item["identity"] is None or item["current_bytes"] is None):
            raise ManagedReportError("invalid verified current accounting")
    return value


def _public(value):
    private = {"store_id", "task_id", "artifact_id", "operation_id", "identity", "origin_generation"}
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in private}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _build(view):
    artifacts = {item["artifact_id"]: item for item in view["artifacts"]}
    unknown = {(item["artifact_id"], item["reason"]) for item in view["unknowns"]}
    if not view["complete"]:
        unknown.add((None, "accounting_view_incomplete"))
    intents, conflicts, duplicates = {}, set(), 0
    for item in view["outcomes"]:
        if item["artifact_id"] not in artifacts:
            raise ManagedReportError("retirement crosses selected task")
        key = (item["operation_id"], item["artifact_id"])
        if key in intents:
            duplicates += 1
            if item != intents[key]:
                conflicts.add(key)
        else:
            intents[key] = item
    removed, uncertain_removal = {}, set()
    for key, outcome in sorted(intents.items()):
        ident = outcome["artifact_id"]
        artifact = artifacts[ident]
        valid = (key not in conflicts and outcome["binding_verified"]
                 and not outcome["operation_id"].startswith(("producer:", "owner:")))
        if not valid:
            unknown.add((ident, "retirement_binding_unverified")); uncertain_removal.add(ident)
        elif outcome["outcome"] == "unlinked":
            if (artifact["identity"] is None or not artifact["creation_verified"]
                    or outcome["removed_bytes"] != artifact["identity"]["size"]):
                unknown.add((ident, "removed_bytes_unverified")); uncertain_removal.add(ident)
            elif ident in removed:
                duplicates += 1  # IDs are never reused; count historical effect once.
            else:
                removed[ident] = dict(outcome, identity=artifact["identity"])
        elif outcome["outcome"] in ("pending", "unknown", "reconciled_missing", "reconciled_present"):
            uncertain_removal.add(ident)
            unknown.add((ident, "removal_causality_unknown"))
    known_created = [item for item in artifacts.values() if item["creation_verified"]]
    generation_unknown = {ident for ident, item in artifacts.items() if not item["creation_verified"]}
    retained, current_unknown = [], set()
    for ident, item in sorted(artifacts.items()):
        if ident in generation_unknown:
            unknown.add((ident, "creation_history_unverified"))
        if item["pending"]:
            uncertain_removal.add(ident)
            unknown.add((ident, "retirement_pending"))
        if item["current_status"] == "present_verified":
            reasons = []
            if item["class"] in schema.PROTECTED_CLASSES or not item["disposable"]:
                reasons.append("protected_or_not_disposable")
            if item["reference_count"]:
                reasons.append("reference_present")
            if item["lease_count"]:
                reasons.append("lease_present")
            if item["pending"]:
                reasons.append("pending_reconciliation")
            if item["retired"]:
                reasons.append("retired_present")
            if view["task_status"] not in ("completed", "failed"):
                reasons.append("task_not_terminal")
            retained.append({"artifact_id": ident, "class": item["class"],
                             "apparent_bytes": item["current_bytes"], "allocated_bytes": item["current_allocated_bytes"],
                             "reasons": reasons or ["remaining_managed_artifact"]})
        elif item["current_status"] == "missing" and ident in removed:
            pass  # Expected absence, with durable confirmed retirement.
        else:
            current_unknown.add(ident)
            unknown.add((ident, "current_presence_unverified"))
            if ident not in removed:
                uncertain_removal.add(ident)
            if item["current_status"] == "missing":
                unknown.add((ident, "missing_without_confirmed_removal"))
    generated_bytes = sum(item["created_bytes"] for item in known_created)
    generated_alloc = sum(item["created_allocated_bytes"] or 0 for item in known_created)
    allocated_generation_unknown = any(item["created_allocated_bytes"] is None for item in known_created)
    retained_bytes = sum(item["apparent_bytes"] for item in retained)
    retained_alloc = sum(item["allocated_bytes"] or 0 for item in retained)
    allocated_retention_unknown = any(item["allocated_bytes"] is None for item in retained)
    removed_bytes = sum(item["removed_bytes"] for item in removed.values())
    items = [{"artifact_id": ident, "reason": reason} for ident, reason in sorted(unknown, key=lambda pair: (pair[0] or "", pair[1]))]
    by_reason = {}
    for item in items:
        by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
    return {"schema": "agent-pooper-scooper/managed-report", "schema_version": 1, "mode": "read-only-accounting",
        "store_id": view["store_id"], "task": {"task_id": view["task_id"], "status": view["task_status"]},
        "observed_at": view["observed_at"], "revision": view["revision"], "recovery_required": view["recovery_required"],
        "generated": {"basis": "durable broker-completed creation facts; not all host writes or capacity savings",
            "known_count": len(known_created), "count": None if generation_unknown else len(known_created),
            "known_apparent_bytes": generated_bytes, "apparent_bytes": None if generation_unknown else generated_bytes,
            "known_allocated_bytes": generated_alloc, "allocated_bytes": None if generation_unknown or allocated_generation_unknown else generated_alloc,
            "unknown_count": len(generation_unknown), "qualification": "known fields are lower bounds when incomplete; allocated sums are per-file metadata, not exclusive physical capacity"},
        "retained": {"basis": "current verified-present registered task files, not a cleanup eligibility grant",
            "known_count": len(retained), "count": None if current_unknown else len(retained),
            "known_apparent_bytes": retained_bytes, "apparent_bytes": None if current_unknown else retained_bytes,
            "known_allocated_bytes": retained_alloc, "allocated_bytes": None if current_unknown or allocated_retention_unknown else retained_alloc,
            "unknown_count": len(current_unknown), "items": retained},
        "removed": {"scope": "cumulative_confirmed_receipts", "count": len(removed),
            "known_bytes": removed_bytes, "bytes": None if uncertain_removal else removed_bytes,
            "unknown_count": len(uncertain_removal), "receipts": list(removed.values()),
            "qualification": "historical confirmed effects, not new removals by this report; collector replay still reports zero new removals"},
        "reclaimed": {"bytes": None, "basis": "unlink receipts do not prove task-specific capacity reclamation"},
        "capacity": {"free_before_bytes": None, "free_after_bytes": None, "basis": "not measured by this read-only ledger report"},
        "unknowns": {"count": len(items), "by_reason": by_reason, "items": items[:128], "truncated": len(items) > 128},
        "audit": {"basis": "read-only projection of existing fenced ledger facts; no new ledger writes",
                  "entries": copy.deepcopy(view["audit"]), "duplicate_facts_suppressed": duplicates},
        "eligible": None,
        "eligibility_basis": "not evaluated by accounting; obtain a fresh managed plan for candidates",
        "notification": {"status": "not_linked", "reason": "delivery is explicit and recorded separately by operation ID"},
        "complete": view["complete"] and not items}


def build_report(authority, task_id, *, public=False):
    try:
        result = _build(_view(authority, task_id))
        return _public(result) if public else result
    except Exception:
        if not public:
            raise
    del authority, task_id
    raise ManagedReportError("managed accounting report refused") from None
