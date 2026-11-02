# Live observations and notification transports

These adapters are development components. Native observation is not a producer
filesystem/reference/resume fence. Cleanup authority remains the closed,
newly-created [managed store protocol](MANAGED_AUTHORITY.md). Existing AO/Kynlo
files, histories, worktrees and unmanaged objects remain KEEP.

## AO

`AoSessionAdapter(session_id, project_id, executable="ao", runner=None,
timeout=5).observe()` invokes exactly `ao session get SESSION --json`, without
a shell. It checks the returned session and project identities. Its bounded
projection contains provider/status, observation time, identity verification
and `can_authorize_cleanup: false`. Display names, branches, paths, activity
and transcripts are not exported. Idle or terminated observations do not
prove that references, resumptions or cleanup are fenced. Identity mismatch,
outage and unknown provider state never become execution authority.

The default subprocess reader bounds stdout, stderr and waiting. It stops only
its own query/bridge child on failure. Kernel I/O itself has no universal hard
wall-clock guarantee. A supplied test runner is not evidence of a live backend;
real integration acceptance must exercise this request and parse path.

## Kynlo

`KynloStateAdapter(call, device_id, mission_id, handle=None)` uses the documented
`get_mission(missionId, metricsOnly=False)` and
`get_agent_session(deviceId, handle)` interfaces. Mission observations check
the returned top-level mission and device IDs; agent observations require an
explicit verified handle and matching returned device/handle. Missing handle
mapping is unsupported, not a reason to select the first listed session.
Every observation remains `can_authorize_cleanup: false`.

There is no default device, endpoint, credential discovery, cookie extraction,
Kynlo executable assumption or native agent control. A trusted host supplies
the explicit callback:

```python
result = call(tool_name, arguments)  # actual MCP CallToolResult dictionary
```

Allowed names are `get_mission`, `get_agent_session`, `notify` and
`get_call_status`. The host uses its already authorized documented interface;
same-account tool availability does not confer production or cleanup authority.
An unconfigured transport refuses effects. Native-global reference/resume
fencing is not supplied by these read APIs.

## Explicit APS bridge contracts

`CommandToolTransport(command, timeout=10)` accepts an explicit argv sequence,
not a shell string. The owner-configured bridge receives one JSON object on
stdin, and returns one actual MCP result object on stdout:

```json
{"tool":"get_mission","arguments":{"missionId":"configured-mission","metricsOnly":false}}
```

This is an APS bridge contract; it does **not** imply that an existing Kynlo
CLI implements it. The configured program must perform the real authorized
tool call. JSON input is limited to 8 KiB, response to 1 MiB, stderr to 16 KiB,
and waiting to the configured bound. Raw command output and provider diagnostics
are not stored in the public observation or delivery receipt.
One monotonic budget starts before request emission and covers nonblocking
write readiness, response reading and child waiting. Ordinary full pipes cannot
block a request before the deadline is considered. All owned child streams are
closed; a killed child can be explicitly reaped asynchronously rather than
extending the caller's expired budget or relying on garbage collection.

`StdioToolTransport(reader=None, writer=None, timeout=30)` is a supervised
request/response callback for an existing MCP host. It emits one bounded line:

```json
{"type":"aps_tool_call","request_id":"unique-nonce","tool":"notify","arguments":{}}
```

The host validates the exact reviewed tool/target/payload, calls its actual
authorized interface once, then returns:

```json
{"request_id":"same-unique-nonce","result":{}}
```

`result` must be the actual tool result, not a manufactured acknowledgement.
The nonce binds the response; malformed, oversized, incomplete and mismatched
responses fail unknown. The deadline applies after first-byte readiness too;
a peer cannot keep a partial JSON line open indefinitely. This bridge does not
install software, introduce a remote endpoint or grant additional effects.
Non-descriptor fallback is restricted to the exact bounded `io.StringIO` memory
stream; arbitrary blocking file-like readers are refused. Native status/state
values must be strings before lookup. Matching identities with malformed lists
or objects yield redacted unknown observations, never exceptions or authority.

## Durable Scoop Report notifications

`KynloNotificationAdapter(call, authority, device_id, mission_id,
test_mode=False).send(operation_id, report)` requires a genuine trusted managed
owner ledger and explicitly configured transport. Call it **after** the
collector's transaction has exited; network delivery is not inside a producer
or deletion fence.

The report input requires `removed.count` and `removed.bytes` (nonnegative
integers, with bytes optionally null for unknown). An optional `eligible` or
`candidate` object contains count and apparent_bytes. Only this numeric
projection creates the message. Filenames, task/producer labels, paths,
host/device/session identifiers, unknown fields and exception details do not
enter its text. Reclaimed capacity is always described as unknown.
Managed read-only reports are distinguished by their versioned schema/mode and
`cumulative_confirmed_receipts` scope. Their fixed message explicitly describes
cumulative confirmed history, known lower-bound bytes and no new removals by the
report. Unknown historical bytes retain an unknown-total statement and bounded
uncertain-outcome count. Free-text qualification fields are never forwarded.
Scope and these numeric facts enter the message and therefore its existing
outbox payload binding; collector-invocation wording stays unchanged.

The target device/mission is configured separately and sent only as routing
metadata. Test mode uses a fixed TEST title and synthetic-fixture label; no
private custom title/body is forwarded.

A FULL SQLite intent commits **before** `notify`. The binding covers exact
operation, target and projected message. Concurrent, repeated or restarted
calls with that key never dispatch again. Changed payload/target refuses.
A process interruption leaves pending, which reports unknown and requires
inspection rather than automatic replay.

The documented native response `Notification sent.` becomes
`provider_acknowledged`, **never user-seen verification**. Unrecognized responses,
exceptions, timeouts and ambiguous errors become unknown. Failure to persist
the post-dispatch result also returns unknown; the original durable pending
row prevents resending. Public delivery receipts omit native IDs and provider
diagnostics.

`reconcile(operation_id)` calls only `get_call_status(callId)` when a real call
ID was returned and durably recorded. It verifies that response identity and
explicit successful completion before upgrading to provider acknowledgement.
When the native response supplies no call ID, reconciliation reports
`call_id_unavailable`. There is no invented status lookup or blind repeat of
`notify`. Read-only reconciliation does not establish that a person saw a
notification.

Unit tests use hermetic callbacks, crash/failure injection and fixture-only
local bridge processes. Standalone native smoke, unit tests, installed product
flow and actual product-to-native callback acceptance are separate evidence.
Additional live tests require exact target/payload/scope authorization. Nothing
here authorizes desktop control, existing user-file cleanup, publishing or
automatic merge/deployment. Runtime dependencies and MIT licensing are unchanged.
