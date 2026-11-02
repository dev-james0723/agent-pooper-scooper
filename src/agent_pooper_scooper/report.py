"""Scoop Report generation (phase 1: observe-only).

Implements the accounting in docs/SCOOP_REPORT.md and docs/SAFETY_MODEL.md.
Nothing is deleted, moved or modified: "eligible" means a dry-run candidate
that passed every check phase 1 can verify, never deletion authority.
"""
from __future__ import annotations

import copy
import os
import re
import stat

from . import __version__, disk, events as task_events, schema

REPORT_SCHEMA = "agent-pooper-scooper/scoop-report"
LIST_CAP = 500
MARKDOWN_ROWS = 50

# Reasons appear in this order; any reason means "retain".
PENDING_CHECKS = (
    "owner_approved_scratch_root",
    "no_active_paused_resumable_or_dependent_references",
    "retention_grace_period",
    "pre_action_identity_recheck",
    "scoped_authorization",
)
NOT_MEASURED = {"missing", "unreadable", "symlink", "symlink_in_path",
                "identity_changed", "changed_during_walk"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _within(path, ancestor):
    """True if ``path`` equals or is lexically inside ``ancestor``."""
    if ancestor == "/":
        return path.startswith("/")
    return path == ancestor or path.startswith(ancestor + "/")


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _path_check(root_path, artifact_path):
    """Reject symlinks in components between a root and an artifact."""
    relative = artifact_path[len(root_path):].strip("/").split("/")[:-1]
    current = root_path
    for component in relative:
        current = current.rstrip("/") + "/" + component
        st = _lstat(current)
        if st is None:
            return "path_unverified"
        if stat.S_ISLNK(st.st_mode):
            return "symlink_in_path"
    return None


class _Collector:
    """Exact per-reason counts with a capped item list."""

    def __init__(self):
        self.count = 0
        self.by_reason = {}
        self.items = []

    def add(self, item):
        self.count += 1
        self.by_reason[item["reason"]] = self.by_reason.get(item["reason"], 0) + 1
        if len(self.items) < LIST_CAP:
            self.items.append(item)

    def merge(self, items, by_reason, context):
        for reason, number in by_reason.items():
            self.by_reason[reason] = self.by_reason.get(reason, 0) + number
            self.count += number
        for item in items:
            if len(self.items) < LIST_CAP:
                self.items.append(dict(item, artifact=context))

    def as_dict(self):
        return {"count": self.count, "by_reason": dict(self.by_reason),
                "items": list(self.items), "items_truncated": self.count > len(self.items)}


class _Audit:
    def __init__(self):
        self.entries = []

    def add(self, action, outcome, subject=None, detail=None):
        entry = {"at": schema.format_timestamp(), "action": action, "outcome": outcome}
        if subject is not None:
            entry["subject"] = subject
        if detail is not None:
            entry["detail"] = detail
        self.entries.append(entry)


def _measure_volumes(paths):
    volumes, errors = {}, []
    for path in paths:
        try:
            usage = disk.volume_usage(path)
        except OSError as exc:
            errors.append({"path": path, "reason": "unreadable", "detail": exc.strerror or "OSError"})
            continue
        volumes.setdefault(usage["device"], dict(usage, path=path))
    return volumes, errors


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def build_report(manifest, events=None, *, public=False,
                 max_entries=disk.DEFAULT_MAX_ENTRIES, max_depth=disk.DEFAULT_MAX_DEPTH,
                 plan=None, evidence_adapter=None, now=None):
    """Build measured accounting, validating linked plans against fresh evidence."""
    try:
        return _build_report(manifest, events, public=public, max_entries=max_entries,
                             max_depth=max_depth, plan=plan, evidence_adapter=evidence_adapter, now=now)
    except Exception:
        if not public:
            raise
    del manifest, events, plan, evidence_adapter
    raise schema.ValidationError(["report refused (details withheld in public mode)"]) from None


def _build_report(manifest, events=None, *, public=False,
                  max_entries=disk.DEFAULT_MAX_ENTRIES, max_depth=disk.DEFAULT_MAX_DEPTH,
                  plan=None, evidence_adapter=None, now=None):
    """Build a Scoop Report dict from a validated manifest and task events."""
    errors = schema.validate_manifest(manifest)
    if errors:
        raise schema.ValidationError(errors)
    planned = {}
    if plan is not None:
        from .adapters import NullEvidenceAdapter
        from .planner import revalidate_plan
        if not revalidate_plan(plan, manifest, evidence_adapter or NullEvidenceAdapter(),
                               now=now, task_events=events)["valid"]:
            raise schema.ValidationError(["plan payload is stale, altered or unverifiable"])
        planned = {item["path"]: item for item in plan["decisions"]}
    audit = _Audit()
    audit.add("report.started", "observe-only", subject=manifest["task_id"],
              detail=f"agent-pooper-scooper {__version__}; phase 1 performs no deletion")
    audit.add("manifest.validated", "valid",
              detail=f"{len(manifest['artifacts'])} artifacts, {len(manifest['roots'])} roots")
    status, status_basis = task_events.task_status(events)
    audit.add("events.evaluated", status, detail=status_basis)

    # Registered roots and their identity.
    roots = []
    for root in manifest["roots"]:
        st = _lstat(root["path"])
        verified = (st is not None and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
                    and (st.st_dev, st.st_ino) == (root["dev"], root["ino"]))
        roots.append({"path": root["path"], "identity_verified": verified})

    volume_paths = [r["path"] for r in roots if r["identity_verified"]]
    if not volume_paths:
        volume_paths = sorted({os.path.dirname(a["path"]) for a in manifest["artifacts"]
                               if _lstat(os.path.dirname(a["path"])) is not None})
    before, volume_errors = _measure_volumes(volume_paths)
    audit.add("volume.measured", "ok" if before else "unmeasured",
              detail=f"before; {len(before)} volume(s)")

    # Policy decisions that do not depend on measuring.
    decisions = []
    for artifact in manifest["artifacts"]:
        reasons = []
        if plan is not None:
            decision = planned.get(artifact["path"])
            if not decision or decision["decision"] != "WOULD_REMOVE":
                reasons.extend(decision["reasons"] if decision else ["plan_missing_artifact"])
        if artifact["class"] in schema.PROTECTED_CLASSES:
            reasons.append("protected_class")
        if not artifact["disposable"]:
            reasons.append("not_marked_disposable")
        if status == "unknown":
            reasons.append("task_status_unknown")
        elif status not in task_events.TERMINAL_STATUSES:
            reasons.append("task_not_terminal")
        containing = [r for r in roots if _within(artifact["path"], r["path"])]
        if not containing:
            reasons.append("outside_registered_root")
        else:
            root = max(containing, key=lambda r: len(r["path"]))
            if not root["identity_verified"]:
                reasons.append("root_identity_unverified")
            else:
                problem = _path_check(root["path"], artifact["path"])
                if problem:
                    reasons.append(problem)
        decisions.append((artifact, reasons))
    retained_paths = [a["path"] for a, reasons in decisions if reasons]
    for artifact, reasons in decisions:
        if reasons:
            continue
        if any(_within(p, artifact["path"]) for p in retained_paths):
            reasons.append("contains_retained_artifact")
        elif any(_within(artifact["path"], p) for p in retained_paths):
            reasons.append("inside_retained_artifact")

    # Measure: retained first, so shared inodes are attributed to "retained".
    seen = set()
    unknowns, skipped = _Collector(), _Collector()
    for item in volume_errors:
        unknowns.add(dict(item, reason="volume_unreadable"))
    retained_items, eligible_items = [], []
    ordered = [d for d in decisions if d[1]] + [d for d in decisions if not d[1]]
    for artifact, reasons in ordered:
        usage = disk.walk_usage(artifact["path"], seen=seen,
                                expect=(artifact["dev"], artifact["ino"]),
                                max_entries=max_entries, max_depth=max_depth)
        unknowns.merge(usage.unknowns, usage.unknowns_by_reason, artifact["path"])
        skipped.merge(usage.skipped, usage.skipped_by_reason, artifact["path"])
        if usage.root_status in NOT_MEASURED:
            continue
        if not reasons and not usage.complete:
            reasons.append("measurement_incomplete")
        item = {
            "path": artifact["path"],
            "class": artifact["class"],
            "disposable": artifact["disposable"],
            "allocated_bytes": usage.allocated_bytes,
            "apparent_bytes": usage.apparent_bytes,
            "files": usage.files,
            "complete": usage.complete,
            "reasons": reasons,
        }
        (retained_items if reasons else eligible_items).append(item)
    audit.add("artifacts.evaluated", "ok",
              detail=f"{len(retained_items)} retained, {len(eligible_items)} eligible, "
                     f"{unknowns.count} unknown, {skipped.count} skipped")

    root_reports = []
    for root, entry in zip(manifest["roots"], roots):
        usage = disk.walk_usage(root["path"], expect=(root["dev"], root["ino"]),
                                max_entries=max_entries, max_depth=max_depth)
        root_reports.append(dict(entry, identity_verified=(entry["identity_verified"]
                                                         and usage.root_status == "measured"),
                                 usage=usage.as_dict()))
        unknowns.merge(usage.unknowns, usage.unknowns_by_reason, root["path"])
        skipped.merge(usage.skipped, usage.skipped_by_reason, root["path"])
    audit.add("roots.measured", "ok", detail=f"{len(root_reports)} root(s)")

    after, after_errors = _measure_volumes([v["path"] for v in before.values()])
    for item in after_errors:
        unknowns.add(dict(item, reason="volume_unreadable"))
    audit.add("volume.measured", "ok" if after else "unmeasured",
              detail=f"after; {len(after)} volume(s)")
    audit.add("cleanup.not_performed", "skipped",
              detail="phase 1 is observe-only; no file was deleted, moved or modified")
    audit.add("notification.not_attempted", "skipped", detail="no notification adapter in phase 1")

    measured = retained_items + eligible_items
    by_class = {}
    for item in measured:
        bucket = by_class.setdefault(item["class"], {"allocated_bytes": 0, "apparent_bytes": 0})
        bucket["allocated_bytes"] += item["allocated_bytes"]
        bucket["apparent_bytes"] += item["apparent_bytes"]
    by_reason = {}
    for item in retained_items:
        for reason in item["reasons"]:
            by_reason[reason] = by_reason.get(reason, 0) + item["allocated_bytes"]
    volumes = []
    for device, first in before.items():
        last = after.get(device)
        volumes.append({
            "device": device,
            "path": first["path"],
            "total_bytes": first["total_bytes"],
            "available_before_bytes": first["available_bytes"],
            "available_after_bytes": last["available_bytes"] if last else None,
            "free_before_bytes": first["free_bytes"],
            "free_after_bytes": last["free_bytes"] if last else None,
            "observed_available_delta_bytes":
                (last["available_bytes"] - first["available_bytes"]) if last else None,
        })

    def total(items, key):
        return sum(i[key] for i in items)

    report = {
        "report_schema": REPORT_SCHEMA,
        "schema_version": 1,
        "generator": f"agent-pooper-scooper {__version__}",
        "generated_at": schema.format_timestamp(),
        "mode": "observe-only",
        "deletion_performed": False,
        "public": public,
        "task": {
            "task_id": manifest["task_id"],
            "host": manifest["host"],
            "producer": manifest["producer"],
            "status": status,
            "status_basis": status_basis,
            "manifest_created_at": manifest["created_at"],
            "manifest_updated_at": manifest["updated_at"],
        },
        "generated": {
            "allocated_bytes": total(measured, "allocated_bytes"),
            "apparent_bytes": total(measured, "apparent_bytes"),
            "by_class": by_class,
            "basis": "manifest-recorded artifacts measured at report time; writes outside "
                     "recorded artifacts are not observed",
            "confidence": "partial" if unknowns.count else "recorded-artifacts",
        },
        "retained": {
            "allocated_bytes": total(retained_items, "allocated_bytes"),
            "apparent_bytes": total(retained_items, "apparent_bytes"),
            "count": len(retained_items),
            "allocated_bytes_by_reason": by_reason,
            "items": retained_items,
        },
        "eligible": {
            "allocated_bytes": total(eligible_items, "allocated_bytes"),
            "apparent_bytes": total(eligible_items, "apparent_bytes"),
            "count": len(eligible_items),
            "status": "dry_run_candidates",
            "pending_checks": list(PENDING_CHECKS),
            "note": "Eligible bytes are candidate file sizes, not guaranteed recoverable capacity, "
                    "and are not deletion authority.",
            "items": eligible_items,
        },
        "removed": {
            "count": 0,
            "allocated_bytes": 0,
            "receipts": [],
            "errors": [],
            "note": "Phase 1 is observe-only. Nothing was deleted.",
        },
        "capacity": {
            "volumes": volumes,
            "reclaimed_estimate": {
                "bytes": 0,
                "basis": "No removal was performed, so nothing was reclaimed by this tool.",
                "qualification": "Any change in available capacity during the report comes from "
                                 "other activity, APFS purgeable space or snapshots, and is not "
                                 "attributed to this task.",
            },
        },
        "roots": root_reports,
        "unknowns": unknowns.as_dict(),
        "skipped": skipped.as_dict(),
        "notification": {"status": "not_attempted",
                         "reason": "phase 1 has no notification adapter"},
        "audit": audit.entries,
    }
    if plan is not None:
        report["planning"] = plan
        report["eligible"]["status"] = "read_only_plan_candidates"
        report["capacity"]["reclaimed_estimate"] = {
            "bytes": None, "basis": "No executor; reclaimed-by-task causality is unverified.",
            "qualification": "Candidate sizes and filesystem deltas are not verified savings."}
        audit.add("plan.linked", "execution_blocked", detail=plan["state_digest"])
    audit.add("report.completed", "ok")
    if public:
        return redact_strict(report) if plan is not None else redact(report, manifest)
    return report


# --------------------------------------------------------------------------
# public redaction
# --------------------------------------------------------------------------
def make_redactor(manifest):
    prefixes = [(root["path"], f"<root-{index}>")
                for index, root in enumerate(manifest["roots"], start=1)]
    prefixes.sort(key=lambda pair: len(pair[0]), reverse=True)
    home = os.path.expanduser("~")
    if home and home != "/" and home != "~":
        prefixes.append((home.rstrip("/"), "~"))

    def redact_text(text):
        for prefix, label in prefixes:
            if _within(text, prefix):
                return label + text[len(prefix):]
        # Unknown, out-of-root paths need a safe fallback too. Drop the
        # entire field for embedded paths, including names containing spaces.
        if text.startswith("/") or re.search(r"(^|[\s(\"'`:=])/", text):
            return "<redacted-path>"
        return text

    return redact_text


def _map_strings(value, function):
    if isinstance(value, str):
        return function(value)
    if isinstance(value, list):
        return [_map_strings(v, function) for v in value]
    if isinstance(value, dict):
        return {k: _map_strings(v, function) for k, v in value.items()}
    return value


def redact(document, manifest):
    """Copy of a report or manifest with home/personal paths and host redacted."""
    result = _map_strings(copy.deepcopy(document), make_redactor(manifest))
    for holder in (result, result.get("task")):
        if isinstance(holder, dict) and "host" in holder:
            holder["host"] = "<redacted-host>"
    if "public" in result:
        result["public"] = True
    return result


def redact_strict(document):
    """Public planner/task-end copy: hide entire paths, filenames and task identities."""
    paths = {}
    fallback = make_redactor({"roots": []})

    def visit(value, key=None):
        if key == "host":
            return "<redacted-host>"
        if key in ("task_id", "producer", "approved_by", "revision"):
            return "<redacted>" if value is not None else None
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        if isinstance(value, str):
            if value.startswith("/"):
                return paths.setdefault(value, f"<path-{len(paths) + 1}>")
            return fallback(value)
        return value

    result = visit(copy.deepcopy(document))
    # Audit subjects can be task identifiers rather than filesystem paths.
    for entry in result.get("audit", []):
        if "subject" in entry:
            entry["subject"] = "<redacted>"
    if "public" in result:
        result["public"] = True
    return result


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------
def format_bytes(value):
    if value is None:
        return "not measured"
    amount = float(value)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(amount) < 1000 or unit == "TB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{value} B"


def _code(text):
    text = str(text).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


def _rows(items, columns):
    lines = [f"| {' | '.join(c[0] for c in columns)} |",
             f"|{'|'.join('---' if not c[2] else '---:' for c in columns)}|"]
    for item in items[:MARKDOWN_ROWS]:
        lines.append("| " + " | ".join(c[1](item) for c in columns) + " |")
    if len(items) > MARKDOWN_ROWS:
        lines.append(f"\n_{len(items) - MARKDOWN_ROWS} more in the JSON report._")
    return lines


def _issue_section(title, data):
    lines = [f"## {title}", "", f"Count: {data['count']}"]
    if data["by_reason"]:
        lines.append("")
        lines.extend(f"- {_code(reason)}: {number}" for reason, number in sorted(data["by_reason"].items()))
    if data["items"]:
        lines.append("")
        lines.extend(_rows(data["items"], [
            ("Path", lambda i: _code(i["path"]), False),
            ("Reason", lambda i: _code(i["reason"]), False),
            ("Detail", lambda i: _code(i["detail"]) if i.get("detail") else "", False),
        ]))
    return lines + [""]


def render_markdown(report):
    task = report["task"]
    lines = [
        f"# Scoop Report: {_code(task['task_id'])}",
        "",
        "> Observe-only (phase 1). **Nothing was deleted.** Eligible bytes are dry-run "
        "candidates, not deletion authority.",
        "",
        f"- Host: {_code(task['host'])}",
        f"- Producer: {_code(task['producer'])}",
        f"- Task status: {_code(task['status'])} ({task['status_basis']})",
        f"- Manifest: created {task['manifest_created_at']}, updated {task['manifest_updated_at']}",
        f"- Generated at: {report['generated_at']} by {report['generator']}",
        f"- Public copy: {'yes (paths redacted)' if report['public'] else 'no'}",
        "",
        "## Summary",
        "",
        "| Metric | Allocated | Apparent |",
        "|---|---:|---:|",
    ]
    for label, key in (("Task-generated (recorded artifacts)", "generated"),
                       ("Retained", "retained"), ("Eligible (dry-run candidates)", "eligible")):
        lines.append(f"| {label} | {format_bytes(report[key]['allocated_bytes'])} "
                     f"| {format_bytes(report[key]['apparent_bytes'])} |")
    lines.append(f"| Removed | {format_bytes(report['removed']['allocated_bytes'])} | — |")
    lines.append(f"| Verified capacity reclaimed | {format_bytes(report['capacity']['reclaimed_estimate']['bytes'])} | — |")
    generated = report["generated"]
    lines += ["", "## Generated", "", f"Basis: {generated['basis']}. Confidence: {_code(generated['confidence'])}.", ""]
    lines += _rows(sorted(generated["by_class"].items()), [
        ("Class", lambda kv: _code(kv[0]), False),
        ("Allocated", lambda kv: format_bytes(kv[1]["allocated_bytes"]), True),
        ("Apparent", lambda kv: format_bytes(kv[1]["apparent_bytes"]), True),
    ])
    item_columns = [
        ("Path", lambda i: _code(i["path"]), False),
        ("Class", lambda i: _code(i["class"]), False),
        ("Allocated", lambda i: format_bytes(i["allocated_bytes"]), True),
    ]
    lines += ["", "## Retained", ""]
    lines += _rows(report["retained"]["items"],
                   item_columns + [("Reasons", lambda i: ", ".join(_code(r) for r in i["reasons"]), False)])
    eligible = report["eligible"]
    lines += ["", "## Eligible", "", eligible["note"],
              "Checks not yet verified in phase 1: " + ", ".join(_code(c) for c in eligible["pending_checks"]) + ".", ""]
    lines += _rows(eligible["items"], item_columns)
    lines += ["", "## Removed", "", report["removed"]["note"], "", "## Disk capacity", ""]
    lines += _rows(report["capacity"]["volumes"], [
        ("Volume", lambda v: _code(v["path"]), False),
        ("Total", lambda v: format_bytes(v["total_bytes"]), True),
        ("Available before", lambda v: format_bytes(v["available_before_bytes"]), True),
        ("Available after", lambda v: format_bytes(v["available_after_bytes"]), True),
    ])
    estimate = report["capacity"]["reclaimed_estimate"]
    lines += ["", f"Reclaimed estimate: {format_bytes(estimate['bytes'])}. {estimate['basis']} "
                  f"{estimate['qualification']}", "", "## Registered roots", ""]
    lines += _rows(report["roots"], [
        ("Root", lambda r: _code(r["path"]), False),
        ("Identity verified", lambda r: "yes" if r["identity_verified"] else "no", False),
        ("Allocated", lambda r: format_bytes(r["usage"]["allocated_bytes"]), True),
        ("Complete", lambda r: "yes" if r["usage"]["complete"] else "no", False),
    ])
    lines.append("")
    lines += _issue_section("Unknowns", report["unknowns"])
    lines += _issue_section("Skipped", report["skipped"])
    if "planning" in report:
        plan = report["planning"]
        lines += ["## Read-only plan", "", "Execution: **BLOCKED**. File evidence is an unauthenticated "
                  "operator assertion; a candidate is never removal authority.", ""]
        lines += _rows(plan["decisions"], [
            ("Path", lambda d: _code(d["path"]), False),
            ("Decision", lambda d: _code(d["decision"]), False),
            ("Reasons", lambda d: ", ".join(_code(r) for r in d["reasons"]), False),
        ])
        lines.append("")
    notification = report["notification"]
    lines += ["## Notification", "", f"Status: {_code(notification['status'])} ({notification['reason']}).",
              "", "## Audit", ""]
    lines += _rows(report["audit"], [
        ("At", lambda e: e["at"], False),
        ("Action", lambda e: _code(e["action"]), False),
        ("Outcome", lambda e: _code(e["outcome"]), False),
        ("Detail", lambda e: e.get("detail", "").replace("|", "\\|"), False),
    ])
    return "\n".join(lines) + "\n"
