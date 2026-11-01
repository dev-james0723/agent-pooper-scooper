"""Explicit observations and opt-in report delivery; never cleanup authority."""
import json
import sys

from . import live_adapters as live, managed_cli, schema


def _transport(args, *, required=False):
    if args.stdio:
        # Routing IDs are necessary protocol data, not redacted report output.
        if args.public:
            raise live.LiveAdapterError("private_stdio_protocol_cannot_be_public")
        return live.StdioToolTransport(timeout=args.timeout_seconds)
    if args.bridge_command_json:
        if len(args.bridge_command_json.encode()) > 8192:
            raise live.LiveAdapterError("explicit_bridge_bound_exceeded")
        command = schema.parse_json(args.bridge_command_json)
        if (not isinstance(command, list) or not 0 < len(command) <= 32
                or any(not isinstance(item, str) or not item or len(item) > 4096 for item in command)):
            raise live.LiveAdapterError("explicit_bridge_command_required")
        return live.CommandToolTransport(command, timeout=args.timeout_seconds)
    if required:
        raise live.LiveAdapterError("transport_unconfigured")
    return None


def run(args):
    try:
        action = args.integration_command
        if action == "observe-ao":
            result = live.AoSessionAdapter(args.session_id, args.project_id, executable=args.executable,
                                          timeout=args.timeout_seconds).observe()
        elif action == "observe-kynlo":
            observer = live.KynloStateAdapter(_transport(args), args.device_id, args.mission_id, args.handle)
            result = observer.observe_agent() if args.agent else observer.observe_mission()
        elif action == "notice" and not args.send:
            result = {"status": "not_attempted", "user_seen_verified": False, "reason": "explicit_send_required"}
        else:
            callback = _transport(args, required=action == "notice")
            notifier = live.KynloNotificationAdapter(callback, managed_cli._owner(args), args.device_id, args.mission_id,
                                                     test_mode=args.test_mode)
            if action == "notice":
                report = schema.parse_json(managed_cli._read_file(args.report, 262144).decode("utf-8"))
                result = notifier.send(args.operation_id, report)
            else:
                result = notifier.reconcile(args.operation_id)
        sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
        return 1 if action in ("notice", "notice-status") and result["status"] == "unknown" else 0
    except Exception:
        print("error: integration refused or outcome unknown; inspect the durable receipt before any retry", file=sys.stderr)
        return 1


def add_commands(commands):
    parser = commands.add_parser("integrations", help="explicit observations and opt-in configured notification transport")
    actions = parser.add_subparsers(dest="integration_command", required=True)
    ao = actions.add_parser("observe-ao", help="bounded native AO observation; no deletion grant")
    ao.add_argument("--session-id", required=True)
    ao.add_argument("--project-id", required=True)
    ao.add_argument("--executable", default="ao")
    ao.add_argument("--timeout-seconds", type=float, default=5)
    ao.add_argument("--public", action="store_true")
    ao.set_defaults(func=run)
    for name in ("observe-kynlo", "notice", "notice-status"):
        action = actions.add_parser(name)
        action.set_defaults(func=run)
        action.add_argument("--device-id", required=True)
        action.add_argument("--mission-id", required=True)
        action.add_argument("--timeout-seconds", type=float, default=30)
        action.add_argument("--public", action="store_true")
        transport = action.add_mutually_exclusive_group()
        transport.add_argument("--stdio", action="store_true", help="PRIVATE host protocol; incompatible with --public")
        transport.add_argument("--bridge-command-json", help="trusted explicit argv JSON; no shell, default endpoint or discovered credentials")
        if name == "observe-kynlo":
            action.add_argument("--agent", action="store_true")
            action.add_argument("--handle", help="explicit independently verified native handle; never selected automatically")
        else:
            action.add_argument("store")
            action.add_argument("--owner-key-file", required=True)
            action.add_argument("--operation-id", required=True)
            action.add_argument("--test-mode", action="store_true")
            if name == "notice":
                action.add_argument("--report", required=True)
                action.add_argument("--send", action="store_true", help="explicitly dispatch; default does not attempt delivery")
