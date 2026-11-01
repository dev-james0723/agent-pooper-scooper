"""Owner-capability files and opaque producer commands for the closed store."""
import base64
from contextlib import contextmanager
import json
import os
import stat
import sys

from . import disk, executor, executor_platform, schema
from .authority import AuthorityError, AuthorityServer, ManagedAuthority, ProducerClient, MAX_BYTES

CAP_BYTES = 8192


def _read_file(path, maximum, *, private=False):
    parent, name = disk._open_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        observed = os.fstat(stream.fileno())
        if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                or (private and (observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) != 0o600))):
            raise AuthorityError("private_regular_capability_file_required" if private else "regular_input_required")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise AuthorityError("input_bound_exceeded")
    return raw


@contextmanager
def _new_output(path):
    """Reserve before any effect; leave incomplete private output on failure."""
    if path is None:
        yield None
        return
    parent, name = disk._open_parent(path)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        executor_platform.sync_directory(parent)
    finally:
        os.close(parent)


def _serialize(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()


def _emit(args, callback):
    with _new_output(getattr(args, "output", None)) as output:
        value = callback()
        raw = _serialize(value)
        if output is None:
            sys.stdout.write(raw.decode())
        else:
            output.write(raw)
    return value


def _capability(path, kind):
    data = schema.parse_json(_read_file(path, CAP_BYTES, private=True).decode("utf-8"))
    value_name = "owner_key" if kind == "owner" else "credentials"
    if (not isinstance(data, dict) or set(data) != {"schema", "schema_version", value_name}
            or data["schema"] != "agent-pooper-scooper/" + kind + "-capability"
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise AuthorityError("capability_schema_refused")
    value = data[value_name]
    if kind == "owner":
        if not isinstance(value, str) or not 32 <= len(value) <= 256:
            raise AuthorityError("capability_schema_refused")
    else:
        if (not isinstance(value, dict) or set(value) != {"task_id", "producer_id", "generation", "token"}
                or any(executor._ID.check(value[key], key) for key in ("task_id", "producer_id"))
                or type(value["generation"]) is not int or value["generation"] < 0
                or not isinstance(value["token"], str) or not 32 <= len(value["token"]) <= 256):
            raise AuthorityError("capability_schema_refused")
    return value


def _envelope(kind, value):
    return {"schema": "agent-pooper-scooper/" + kind + "-capability", "schema_version": 1,
            "owner_key" if kind == "owner" else "credentials": value}


def _owner(args):
    return ManagedAuthority(args.store, owner_key=_capability(args.owner_key_file, "owner"))


def _plan(args):
    return executor.load_plan_text(_read_file(args.plan, executor.MAX_PLAN_BYTES).decode("utf-8"))


def _producer(args):
    client = ProducerClient(args.socket, _capability(args.credentials_file, "producer") if args.credentials_file else None)
    action = args.producer_command
    if action in ("register", "resume"):
        # Reserve capability destination before the authenticated generation
        # transition. Secrets only enter protocol frames and this private file.
        with _new_output(args.credentials_output) as destination:
            key = _capability(args.owner_key_file, "owner")
            params = {"owner_key": key, "task_id": args.task_id if action == "register" else client.credentials["task_id"]}
            if action == "register":
                params.update(producer_id=args.producer_id, grace_seconds=args.grace_seconds)
            credentials = client.request(action, params, args.request_id)
            destination.write(_serialize(_envelope("producer", credentials)))
        return {"status": "credentials_written"}
    if client.credentials is None:
        raise AuthorityError("producer_capability_file_required")
    if action == "read":
        with _new_output(args.output) as destination:
            response = client.request("read", {"artifact_id": args.artifact_id})
            raw = base64.b64decode(response["bytes"], validate=True)
            if len(raw) > MAX_BYTES:
                raise AuthorityError("response_bound_exceeded")
            destination.write(raw)
        return {"status": "read_copy_written", "bytes": len(raw)}
    if action == "write":
        raw = _read_file(args.input, MAX_BYTES)
        ident = client.request("write", {"data": base64.b64encode(raw).decode(),
                                         "artifact_class": args.artifact_class, "disposable": args.disposable}, args.request_id)
        return {"artifact_id": ident}
    params = {"start": {}, "terminal": {"status": getattr(args, "status", "completed")},
              "set-state": {"status": getattr(args, "status", None)},
              "reference": {"artifact_id": getattr(args, "artifact_id", None), "claim_id": getattr(args, "claim_id", None)},
              "lease": {"artifact_id": getattr(args, "artifact_id", None), "lease_id": getattr(args, "claim_id", None),
                        "expires_at": getattr(args, "expires_at", None)},
              "release": {"claim_id": getattr(args, "claim_id", None)}}[action]
    value = client.request("set_state" if action == "set-state" else action, params, args.request_id)
    return {"claim_id": value} if action in ("reference", "lease") else value


def run(args):
    """Keep every managed error path content-free, including non-public mode."""
    try:
        action = args.managed_command
        if action == "init":
            with _new_output(args.owner_key_output) as destination:
                owner = ManagedAuthority.create(args.store)
                destination.write(_serialize(_envelope("owner", owner.owner_key)))
            print('{"status":"created","owner_capability":"private_file"}')
        elif action == "serve":
            server = AuthorityServer(_owner(args), socket_path=args.socket)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.close()
        elif action == "producer":
            value = _producer(args)
            # read output contains bytes, never mix it with JSON emission.
            if args.public:
                value = {"status": "completed"}
            sys.stdout.write(_serialize(value).decode())
        elif action == "schema":
            sys.stdout.write(_serialize(executor.plan_schema()).decode())
        else:
            owner = _owner(args)
            if action == "report":
                from . import managed_report
                _emit(args, lambda: managed_report.build_report(owner, args.task_id, public=args.public))
            elif action == "plan":
                _emit(args, lambda: executor.create_plan(owner, args.task_id, artifact_ids=args.artifact_id,
                                                        deadline_seconds=args.deadline_seconds, public=args.public))
            elif action == "approve":
                def approve():
                    grant = executor.approve_plan(owner, _plan(args), args.operation_id,
                                                  ttl_seconds=args.ttl_seconds, deadline_seconds=args.deadline_seconds)
                    return {"status": "owner_approval_recorded", "expires_at": grant["expires_at"]} if args.public else grant
                _emit(args, approve)
            elif action == "collect":
                result = _emit(args, lambda: executor.collect(owner, _plan(args), execute=args.execute,
                              operation_id=args.operation_id, approval_id=args.approval_id,
                              deadline_seconds=args.deadline_seconds, public=args.public))
                if result["status"] not in ("DRY_RUN", "COMPLETED", "IDEMPOTENT_REPLAY"):
                    return 1
            elif action == "reconcile":
                def reconcile():
                    result = owner.reconcile(args.operation_id, args.artifact_id)
                    return {"outcome": result["outcome"], "causality": "unknown", "removed_bytes": None} if args.public else result
                _emit(args, reconcile)
            elif action == "status":
                result = owner.pending_operations()
                _emit(args, lambda: {"pending_count": len(result)} if args.public else {"pending_operations": result})
        return 0
    except Exception:
        # Do not retain/chase raw third-party or input exceptions. Durable
        # operation IDs/receipts are the recovery route after ambiguous effects.
        print("error: managed operation refused or outcome uncertain; inspect owner status and receipts before retrying", file=sys.stderr)
        return 1


def add_commands(commands):
    managed = commands.add_parser("managed", help="closed APS-created store; no existing data adoption")
    sub = managed.add_subparsers(dest="managed_command", required=True)

    def leaf(name, help_text, owner=False, output=True):
        parser = sub.add_parser(name, help=help_text)
        parser.set_defaults(func=run)
        parser.add_argument("--public", action="store_true")
        if owner:
            parser.add_argument("store")
            parser.add_argument("--owner-key-file", required=True)
        if output:
            parser.add_argument("--output", help="new private JSON file; reserved before effects")
        return parser

    init = leaf("init", "create fresh physical store and private owner key", output=False)
    init.add_argument("store")
    init.add_argument("--owner-key-output", required=True)
    serve = leaf("serve", "serve producer protocol in foreground", owner=True, output=False)
    serve.add_argument("--socket")
    leaf("schema", "strict managed plan schema", output=False)
    leaf("status", "discover pending owner reconciliation keys", owner=True)
    reconcile = leaf("reconcile", "inspect pending identity; never repeat deletion", owner=True)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--artifact-id", required=True)
    accounting = leaf("report", "read-only unified managed task accounting; no state transition", owner=True)
    accounting.add_argument("--task-id", required=True)
    plan = leaf("plan", "dry-run managed objects under broker fence", owner=True)
    plan.add_argument("--task-id", required=True)
    plan.add_argument("--artifact-id", action="append")
    plan.add_argument("--deadline-seconds", type=float, default=15)
    approve = leaf("approve", "explicit owner grant bound to reviewed plan", owner=True)
    approve.add_argument("--plan", required=True)
    approve.add_argument("--operation-id", required=True)
    approve.add_argument("--ttl-seconds", type=int, default=30)
    approve.add_argument("--deadline-seconds", type=float, default=15)
    collect = leaf("collect", "dry-run default; explicit grant required for execution", owner=True)
    collect.add_argument("--plan", required=True)
    collect.add_argument("--operation-id")
    collect.add_argument("--approval-id")
    collect.add_argument("--execute", action="store_true")
    collect.add_argument("--deadline-seconds", type=float, default=15)

    producer = sub.add_parser("producer", help="opaque broker producer operations")
    producer.add_argument("--socket", required=True)
    producer.add_argument("--credentials-file")
    actions = producer.add_subparsers(dest="producer_command", required=True)
    for name in ("register", "resume", "start", "write", "read", "reference", "lease", "release", "set-state", "terminal"):
        action = actions.add_parser(name)
        action.set_defaults(func=run)
        action.add_argument("--public", action="store_true")
        if name != "read":
            action.add_argument("--request-id", required=True, help="stable exact-payload retry key; never blindly retry unknown outcomes")
        if name in ("register", "resume"):
            action.add_argument("--owner-key-file", required=True)
            action.add_argument("--credentials-output", required=True)
        if name == "register":
            action.add_argument("--task-id", required=True)
            action.add_argument("--producer-id", required=True)
            action.add_argument("--grace-seconds", type=int, default=1)
        if name == "write":
            action.add_argument("--input", required=True)
            action.add_argument("--class", dest="artifact_class", choices=schema.ARTIFACT_CLASSES, default="temp")
            action.add_argument("--disposable", action="store_true", help="explicitly disposable; default retains")
        if name in ("read", "reference", "lease"):
            action.add_argument("--artifact-id", required=True)
        if name == "read":
            action.add_argument("--output", required=True, help="new private file for copied bytes")
        if name in ("reference", "lease", "release"):
            action.add_argument("--claim-id", required=name == "release")
        if name == "lease":
            action.add_argument("--expires-at")
        if name in ("set-state", "terminal"):
            action.add_argument("--status", choices=("paused", "resumable") if name == "set-state" else ("completed", "failed"),
                                required=name == "set-state", default=None if name == "set-state" else "completed")
