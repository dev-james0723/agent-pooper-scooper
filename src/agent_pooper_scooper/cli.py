"""``aps`` CLI: legacy read-only reports and explicit closed-store collection.

Exit codes: 0 success, 1 validation/refusal, 2 usage error.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stderr
import io
import json
import os
import socket
import sys

from . import __version__, adapters, disk, events, manifest, planner, report, schema


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def _print_errors(errors, public=False):
    if public:
        print("error: operation refused (details withheld in public mode)", file=sys.stderr)
        return
    for message in errors:
        print(f"error: {message}", file=sys.stderr)


def _cmd_init(args):
    host = args.host or socket.gethostname()
    outcome = manifest.init_manifest(args.manifest, task_id=args.task_id, producer=args.producer,
                                     host=host, roots=args.root)
    print(f"{outcome}: {args.manifest}")
    return 0


def _cmd_record(args):
    outcome = manifest.record_artifact(args.manifest, args.path, artifact_class=args.artifact_class,
                                       disposable=args.disposable)
    print(f"{outcome}: {os.path.abspath(args.path)}")
    return 0


def _cmd_validate(args):
    document = manifest.load_manifest(args.manifest)
    print(f"valid: {args.manifest} ({len(document['artifacts'])} artifacts, "
          f"{len(document['roots'])} roots)")
    return 0


def _cmd_show(args):
    document = manifest.load_manifest(args.manifest)
    if args.public:
        document = report.redact(document, document)
    sys.stdout.write(manifest.dumps(document))
    return 0


def _cmd_event_validate(args):
    loaded = events.load_events_file(args.events, args.task_id)
    print(f"{len(loaded)} valid events: {args.events}")
    return 0


def _cmd_schema(args):
    sys.stdout.write(json.dumps(schema.json_schema(args.kind), indent=2) + "\n")
    return 0


def _cmd_telemetry(args):
    document = manifest.load_manifest(args.manifest)
    roots, volumes, unknowns, seen = [], {}, [], set()
    for root in document["roots"]:
        usage = disk.walk_usage(root["path"], seen=seen, expect=(root["dev"], root["ino"]))
        roots.append({"path": root["path"], "usage": usage.as_dict()})
        if usage.root_status != "measured":
            continue
        try:
            volume = disk.volume_usage(root["path"])
            volumes.setdefault(volume["device"], dict(volume, path=root["path"]))
        except OSError:
            unknowns.append({"path": root["path"], "reason": "volume_unreadable"})
    data = {"mode": "observe-only", "roots": roots, "volumes": list(volumes.values()),
            "unknowns": unknowns,
            "complete": not unknowns and all(r["usage"]["complete"] for r in roots)}
    if args.public:
        data = report.redact(data, document)
    sys.stdout.write(manifest.dumps(data))
    return 0


def _write_new(path, text):
    """Write a new private file; never overwrite or follow a symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd, name = disk._open_parent(path)
    try:
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            raise manifest.ManifestError(f"{path}: output file exists; refusing to overwrite") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.close(parent_fd)


def _cmd_report(args):
    document = manifest.load_manifest(args.manifest)
    loaded = events.load_events_file(args.events, document["task_id"]) if args.events else None
    data = report.build_report(document, loaded, public=args.public)
    if args.format == "markdown":
        text = report.render_markdown(data)
    else:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _emit(text, args.output, args.public)
    return 0


def _emit(text, output, public):
    if output:
        _write_new(output, text)
        print("wrote: <redacted-path>" if public else f"wrote: {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _evidence_adapter(args):
    return adapters.FileEvidenceAdapter(args.evidence) if args.evidence else adapters.NullEvidenceAdapter()


def _cmd_plan(args):
    document = manifest.load_manifest(args.manifest)
    data = planner.build_plan(document, _evidence_adapter(args), public=args.public)
    _emit(manifest.dumps(data), args.output, args.public)
    return 1 if args.execute else 0


def _cmd_task_end(args):
    document = manifest.load_manifest(args.manifest)
    loaded = events.load_events_file(args.events, document["task_id"])
    adapter = _evidence_adapter(args)
    plan = planner.build_plan(document, adapter, task_events=loaded)
    data = report.build_report(document, loaded, plan=plan, evidence_adapter=adapter)
    data["notification"] = dict(adapters.NullNotificationAdapter().status(),
                                reason="no verified live notification interface")
    if args.public:
        data = report.redact_strict(data)
    text = report.render_markdown(data) if args.format == "markdown" else manifest.dumps(data)
    _emit(text, args.output, args.public)
    return 0


def build_parser():
    parser = _Parser(
        prog="aps",
        description="Agent Pooper Scooper: read-only reports and explicit owner-approved "
                    "collection inside newly created managed stores.")
    parser.add_argument("--version", action="version", version=f"aps {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    manifest_parser = commands.add_parser("manifest", help="create, update, validate or show a task manifest")
    manifest_commands = manifest_parser.add_subparsers(dest="manifest_command", metavar="ACTION", required=True)

    init = manifest_commands.add_parser("init", help="create a manifest (idempotent)")
    init.add_argument("manifest")
    init.add_argument("--task-id", required=True)
    init.add_argument("--producer", required=True, help="agent or tool that produced the task")
    init.add_argument("--host", help="host name (default: this host)")
    init.add_argument("--root", action="append", default=[],
                      help="register a task root directory (repeatable)")
    init.set_defaults(func=_cmd_init)

    record = manifest_commands.add_parser("record", help="record an artifact's identity (idempotent)")
    record.add_argument("manifest")
    record.add_argument("path")
    record.add_argument("--class", dest="artifact_class", required=True, choices=schema.ARTIFACT_CLASSES)
    record.add_argument("--disposable", action="store_true",
                        help="mark as disposable scratch (default: keep); protected classes refuse this")
    record.set_defaults(func=_cmd_record)

    validate = manifest_commands.add_parser("validate", help="strictly validate a manifest")
    validate.add_argument("manifest")
    validate.set_defaults(func=_cmd_validate)

    show = manifest_commands.add_parser("show", help="print a validated manifest")
    show.add_argument("manifest")
    show.add_argument("--public", action="store_true", help="redact home/personal paths and host")
    show.set_defaults(func=_cmd_show)

    event_parser = commands.add_parser("event", help="task event logs (JSON Lines)")
    event_commands = event_parser.add_subparsers(dest="event_command", metavar="ACTION", required=True)
    event_validate = event_commands.add_parser("validate", help="strictly validate an event log")
    event_validate.add_argument("events")
    event_validate.add_argument("--task-id", help="require every event to belong to this task")
    event_validate.set_defaults(func=_cmd_event_validate)

    schema_parser = commands.add_parser("schema", help="print a versioned JSON Schema")
    schema_parser.add_argument("kind", choices=("task-event", "manifest", "planner-evidence"))
    schema_parser.set_defaults(func=_cmd_schema)

    telemetry = commands.add_parser("telemetry", help="measure registered roots and volume capacity")
    telemetry.add_argument("manifest")
    telemetry.add_argument("--public", action="store_true", help="redact home/personal paths")
    telemetry.set_defaults(func=_cmd_telemetry)

    report_parser = commands.add_parser("report", help="generate an observe-only Scoop Report")
    report_parser.add_argument("manifest")
    report_parser.add_argument("--events", help="task event log (JSON Lines); without it status is unknown")
    report_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    report_parser.add_argument("--public", action="store_true", help="redact home/personal paths and host")
    report_parser.add_argument("--output", help="write to a new file (never overwrites)")
    report_parser.set_defaults(func=_cmd_report)

    plan_parser = commands.add_parser("plan", help="conservative read-only regular-file plan")
    plan_parser.add_argument("manifest")
    plan_parser.add_argument("--evidence", help="explicit operator evidence JSON; absent means KEEP")
    plan_parser.add_argument("--public", action="store_true")
    plan_parser.add_argument("--output", help="write only a new output file")
    plan_parser.add_argument("--execute", action="store_true", help="always BLOCKED; returns exit 1")
    plan_parser.set_defaults(func=_cmd_plan)

    task_end = commands.add_parser("task-end", help="file-event task-end report with conservative planning")
    task_end.add_argument("manifest")
    task_end.add_argument("--events", required=True)
    task_end.add_argument("--evidence", help="operator evidence JSON; no live provider is assumed")
    task_end.add_argument("--public", action="store_true")
    task_end.add_argument("--format", choices=("json", "markdown"), default="json")
    task_end.add_argument("--output")
    task_end.set_defaults(func=_cmd_task_end)
    from . import managed_cli
    managed_cli.add_commands(commands)
    from . import integration_cli
    integration_cli.add_commands(commands)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    public_forms = {"--public"[:n] for n in range(3, len("--public") + 1)}
    public = any(arg.split("=", 1)[0] in public_forms for arg in argv)
    parser = build_parser()
    try:
        # Argparse can echo arbitrary user values before a manifest is loaded.
        with redirect_stderr(io.StringIO()) if public else nullcontext():
            args = parser.parse_args(argv)
    except SystemExit as exc:
        if public and exc.code:
            _print_errors([], public=True)
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return args.func(args)
    except schema.ValidationError as exc:
        _print_errors(exc.errors, public)
    except manifest.ManifestError as exc:
        _print_errors([str(exc)], public)
    except OSError as exc:
        _print_errors([f"{exc.filename or ''}: {exc.strerror or exc}"], public)
    except Exception:
        if not public:
            raise
        _print_errors([], public=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
