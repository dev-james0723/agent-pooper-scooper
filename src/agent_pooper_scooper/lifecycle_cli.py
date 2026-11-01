"""Explicit private setup and continuation of the reviewed cooperating SDK."""
import os
import sys

from . import integration_cli, managed_cli
from .managed_lifecycle import ManagedTaskCoordinator, _journal_parent, public_result


def _outside_output(owner, coordinator, path):
    if path is None:
        return
    # Both objects have already been authenticated. Reuse the reviewed pure
    # ancestry observation only to DENY unsafe output placement, never as a grant.
    for root in (owner, coordinator):
        parent, _ = _journal_parent(root, path)
        os.close(parent)


def _emit(args, owner, coordinator, callback):
    path = getattr(args, "output", None)
    _outside_output(owner, coordinator, path)
    with managed_cli._new_output(path) as output:
        value = callback()
        raw = managed_cli._serialize(value)
        if output is None:
            sys.stdout.write(raw.decode())
        else:
            output.write(raw)
    return value


def run(args):
    try:
        owner = managed_cli._owner(args)
        action = args.lifecycle_command
        if action == "init":
            ManagedTaskCoordinator.create(owner, args.socket, args.journal)
            print('{"status":"created","journal":"private"}')
            return 0
        # An absent, foreign or incompatible journal never becomes create().
        coordinator = ManagedTaskCoordinator(owner, args.socket, args.journal)
        if action in ("enroll", "resume"):
            _outside_output(owner, coordinator, args.credentials_output)
            with managed_cli._new_output(args.credentials_output) as output:
                if action == "enroll":
                    client = coordinator.enroll(args.task_id, args.producer_id, args.request_id,
                                                grace_seconds=args.grace_seconds)
                else:
                    client = coordinator.resume(args.task_id, args.request_id)
                output.write(managed_cli._serialize(managed_cli._envelope("producer", client.credentials)))
            print('{"status":"credentials_written"}')
            return 0
        if action == "status":
            result = _emit(args, owner, coordinator, coordinator.status)
            return 1 if result["status"] == "UNKNOWN" else 0
        # Configuration does not construct a transport without explicit send.
        callback = integration_cli._transport(args, required=True) if args.send else None
        if args.send and (not args.device_id or not args.mission_id):
            raise ValueError("explicit notification target required")
        plan = managed_cli._plan(args) if args.plan else None
        def task_end():
            result = coordinator.task_end(args.task_id, args.request_id, execute=args.execute, plan=plan,
                                          approval_id=args.approval_id, operation_id=args.operation_id,
                                          send=args.send, callback=callback, device_id=args.device_id,
                                          mission_id=args.mission_id)
            return public_result(result)
        result = _emit(args, owner, coordinator, task_end)
        return 1 if result.get("delivery", {}).get("status") == "unknown" else 0
    except Exception:
        print("error: lifecycle refused or outcome unknown; inspect the existing journal and owner receipts before retrying", file=sys.stderr)
        return 1


def add_commands(commands):
    lifecycle = commands.add_parser("lifecycle", help="explicit journal setup and cooperating SDK continuation; no native authority")
    actions = lifecycle.add_subparsers(dest="lifecycle_command", required=True)
    for name in ("init", "enroll", "resume", "task-end", "status"):
        action = actions.add_parser(name)
        action.set_defaults(func=run)
        action.add_argument("store")
        action.add_argument("--owner-key-file", required=True)
        action.add_argument("--socket", required=True)
        action.add_argument("--journal", required=True)
        action.add_argument("--public", action="store_true")
        if name in ("enroll", "resume", "task-end"):
            action.add_argument("--task-id", required=True)
            action.add_argument("--request-id", required=True)
        if name in ("enroll", "resume"):
            action.add_argument("--credentials-output", required=True, help="new private file outside store and journal")
        if name == "enroll":
            action.add_argument("--producer-id", required=True)
            action.add_argument("--grace-seconds", type=int, default=1)
        if name in ("task-end", "status"):
            action.add_argument("--output", help="new private output outside store and journal")
        if name == "task-end":
            action.add_argument("--execute", action="store_true")
            action.add_argument("--plan")
            action.add_argument("--approval-id")
            action.add_argument("--operation-id")
            action.add_argument("--send", action="store_true")
            action.add_argument("--device-id")
            action.add_argument("--mission-id")
            action.add_argument("--timeout-seconds", type=float, default=30)
            transport = action.add_mutually_exclusive_group()
            transport.add_argument("--stdio", action="store_true", help="PRIVATE host protocol; never combine with --public")
            transport.add_argument("--bridge-command-json", help="explicit trusted argv JSON, not a shell command")
