"""User-facing native inventory and volume-pressure commands."""
import json

from . import host_report


def run(args):
    # Import lazily: cli registers this module's commands during parser creation.
    from .cli import _emit
    options = {"public": args.public, "min_free_bytes": args.min_free_bytes,
               "min_free_percent": args.min_free_percent}
    if args.host_command == "scan":
        data = host_report.scan(args.root, max_entries=args.max_entries,
                                max_depth=args.max_depth, **options)
    else:
        data = host_report.pressure(args.path, **options)
    text = (host_report.render_markdown(data) if args.format == "markdown"
            else json.dumps(data, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
    _emit(text, args.output, args.public)
    # Partial measurements and pressure warnings are report data, not a failed
    # command. Invalid inputs/output failures still use the main CLI errors.
    return 0


def add_commands(commands):
    host = commands.add_parser("host", help="read-only native disk inventory and pressure")
    actions = host.add_subparsers(dest="host_command", required=True)
    for name, help_text in (("scan", "measure named native locations; keep every existing file"),
                            ("pressure", "check volume capacity without walking directories")):
        command = actions.add_parser(name, help=help_text)
        command.set_defaults(func=run)
        command.add_argument("--public", action="store_true", help="omit all native paths and file details")
        command.add_argument("--format", choices=("json", "markdown"), default="json")
        command.add_argument("--output", help="write a new private report; never overwrite")
        command.add_argument("--min-free-bytes", type=int, default=host_report.DEFAULT_MIN_FREE_BYTES)
        command.add_argument("--min-free-percent", type=float, default=host_report.DEFAULT_MIN_FREE_PERCENT)
        if name == "scan":
            command.add_argument("--root", action="append", help="specific physical directory (repeatable); defaults to existing common agent locations")
            command.add_argument("--max-entries", type=int, default=host_report.DEFAULT_MAX_ENTRIES,
                                 help="entry limit per root (default: 20000; maximum: 100000)")
            command.add_argument("--max-depth", type=int, default=host_report.DEFAULT_MAX_DEPTH)
        else:
            command.add_argument("--path", help="path on the volume to check (default: home volume)")
