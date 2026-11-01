# Managed lifecycle SDK — local development component

This component is pending exact independent source and runtime review. It is
not a native AO/Kynlo producer integration, installation or release. Existing
user/native roots remain unmanaged. The current reviewed draft is preserved.

`ManagedTaskCoordinator.create(owner, socket_path, journal_path)` exclusively
creates a private workflow journal outside an existing **APS-created** store.
The owner is a genuine `ManagedAuthority`; its broker must already be serving.
Opening an existing journal uses `ManagedTaskCoordinator(...)`, never `create`.
No new daemon, arbitrary command runner, dependency or stdin transport is added.

## Explicit CLI setup and continuation

The CLI has a separate journal initialization command. All other lifecycle
commands open an existing validated journal; failed open never becomes create.
The existing owner broker remains the foreground `aps managed serve` process.

```sh
aps managed lifecycle init /physical/store --owner-key-file /physical/owner.json \
  --socket /physical/rpc.sock --journal /physical/new-workflow
aps managed lifecycle enroll /physical/store --owner-key-file /physical/owner.json \
  --socket /physical/rpc.sock --journal /physical/new-workflow \
  --task-id example --producer-id cooperating-client --request-id enroll-example \
  --credentials-output /physical/new-producer.json
aps managed lifecycle status /physical/store --owner-key-file /physical/owner.json \
  --socket /physical/rpc.sock --journal /physical/new-workflow --public
```

These setup commands do not run arbitrary programs or enroll native sessions.
The trusted host runs its actual cooperating callable through `run_producer`.
Normal completion automatically creates the report; the owner can then inspect
that same immutable preview using `lifecycle task-end --task-id ...
--request-id ...`. A new `--output` selects a private report file. Credential
and report outputs must be outside **both** the managed store and workflow
journal. The wrapper authenticates owner/journal first, applies the reviewed
physical ancestry guard only as a DENY check, and reserves exclusive 0600
outputs before enrollment, resume, collection or delivery.

Later approved collection uses the existing `managed plan` and `managed approve`
commands. Pass the exact unredacted plan, approval ID and operation ID to
`lifecycle task-end --execute`. Default evaluation does not refresh grants.
An optional `--send` additionally requires the explicit device/mission and a
configured `--bridge-command-json` or private `--stdio` callback. Configuration
alone never constructs a transport or sends. `--stdio --public` refuses before
protocol construction. Output errors/refusals are generic; inspect the durable
journal and owner receipts after an uncertain effect instead of changing keys.

`lifecycle resume` requires owner authentication and a newly selected private
credential-output file; it never prints producer credentials. There is no CLI
shell/module runner: producer work remains an explicitly cooperating SDK call.
The CLI is covered by focused fixture tests; fresh installed acceptance is a
separate gate, not inferred from those tests or this usage example.

## Primary producer flow

The trusted launcher calls `enroll(task_id, producer_id, request_id)` and gives
the cooperating callable only the resulting restricted `ProducerClient`.
`run_producer(client, callable, request_id)` starts that producer, invokes it
once, records known authenticated terminal completion, then **automatically**
reaches `task_end` and a sealed qualified report. The caller does not manually
chain terminal/report operations. A supported explicit failed completion uses
`completion_status="failed"`. An explicit paused/resumable return stays KEEP;
unexpected exceptions/death leave the invocation unknown, without inventing a
terminal state or rerunning the callable.

The callable generates primary bytes directly through `ProducerClient.write`.
There is no unmanaged input artifact whose deletion is claimed. Existing
`--input` remains a copy operation and cannot reclaim its original. All reads,
writes, references, leases and releases must use the same broker. Its flat
512-KiB/object and 1,024-registration limits still apply. Python callables and
mode 0700 are not a sandbox or hostile same-UID credential isolation.

An illustrative cooperating producer can compute a bounded result in memory
and call `client.write(result, disposable=True)` while separately retaining
protected outputs with `disposable=False`. It must not declare its own terminal
transition inside `run_producer`; the owner-side runner records the known
completion. Native session status/PID/preflight is not that completion.

## Identities and continuations

Workflow identity is store ID, task ID and enrollment generation. Request IDs
are exact-payload replay aliases, not new workflows or effect permission.
The outer task-end request binds preview/collection/delivery parameters before
effects. Internal substages have separate alias domains, so one approved
collection-and-delivery request can complete without colliding with itself.
Canonical effect, report and delivery slots do not change with those aliases.
Registration/resume replay keys also bind the authenticated journal ID. A
fresh/unbound journal cannot recover or adopt another journal's enrollment.

The canonical preview phase seals revision 0. Repeated requests return its
original report bytes, including its original observation timestamp. Preview
or awaiting-approval status does not finish the workflow. A later separately
approved operation creates its own collection phase and immutable report
revision; it cannot overwrite preview/history. Repeated collection requests
report zero new removals while retaining the sealed cumulative report.

`task_end(..., execute=True, plan=..., approval_id=..., operation_id=...)`
requires the existing fresh exact owner grant and executor revalidation.
Missing/stale grants do not authorize cleanup or refresh an approval. Active,
paused/resumable tasks, references and even expired unreleased leases retain.
Owner `resume(task_id, request_id)` uses the existing generation transition;
aliases after a known resume retrieve the same credentials rather than rotate
again. Old credentials and retired IDs are never restored.

## Journal and crash boundary

The exclusive directory is 0700 and files are 0600. Reopen validates physical
no-follow root/database identity, ownership, format, store-root binding and
owner HMAC. Every record is authenticated and bound to its journal and key.
Journal format 2 refuses incompatible format-1 development journals; it never
migrates or adopts their former alias bindings. Before any journal access, the
existing public `owner.pending_operations()` validates current owner lifecycle,
including inherited-owner refusal, without recording its returned metadata.
This logically read-only guard uses the existing authority connection behavior.
It runs before journal transactions; post-yield identity checks do not call the
broker. Before mkdir, a bounded physical parent-FD walk rejects store/control/
artifacts ancestry and case aliases by device/inode identity. Unknown ancestry
refuses; no cleanup or repair of an unknown managed entry is attempted.
The implementation uses documented trusted-owner `owner_key`/`path` attributes
and existing public register/snapshot/resume/receipt APIs; it never reads or
edits authority SQL/private state. No authority API or effect primitive changes.

Fixed schemas and bounds refuse unknown or excessive state: at most 128
workflows, 4,096 records, a 2-MiB sealed report and a 16-MiB journal payload.
Producer READ/WRITE bodies, native response bodies, credentials, owner keys and
exception text are not cached. The bounded trusted accounting report is an
intentional stored DTO. Public projection omits workflow/phase/target IDs.

SQLite phase claims serialize competing callers. Intent commits precede
invocation, terminal/resume/collection and dispatch. No journal transaction
spans broker or network calls; no atomicity across databases is claimed.
Resume atomically rechecks workflow exclusivity when claiming its phase, before
entering the broker, including a completed-credential replay. This checks all
pending or unknown phases in the journal, including the current generation;
the cached predecessor binding and broker replay key stay immutable.
Unconfirmed stage completion stops mutation and remains inspect-only. A fresh
request ID, timestamp, grant refresh or replacement journal cannot hide it.
Underlying authority recovery remains authoritative. Status does not repair
or reset a pending operation. Read the existing owner receipts/accounting and
notifier status for inspection; no automatic effect reconciliation is added.

## Delivery

Default report generation never sends. An explicit configured callback plus
`send=True` uses the existing notifier after the producer/collector fence ends.
There is exactly one delivery slot per workflow and canonical report phase.
Target, sealed-report hash and numeric message are pinned **data inside that
slot**, not extra key columns. Different targets/payloads refuse even after an
unknown result. Repeated aliases cannot resend. Unknown delivery also prevents
new collection/resume; it is not cleared by creating a later phase.

Reports preserve cumulative confirmed history, known lower bounds, unknown
totals, no-new-removal qualification and null reclaimed capacity. Provider ACK
is never user-seen verification. All development callbacks are hermetic; this
component provides no new live notification proof or permission.

## Remaining delivery and external gaps

The SDK and explicit CLI can orchestrate an explicitly cooperating producer,
not ordinary native processes
using arbitrary paths or TMPDIR. Real AO/Kynlo producer hooks must enroll at
artifact creation and mediate every access/reference/resume. Checked native
observation, bounded-command and notification contracts do not establish that
common authority. The original full native cleanup mission remains incomplete.
No publication, global installation, native effects or user-file cleanup is
authorized by this document.
