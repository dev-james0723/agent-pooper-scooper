# Closed managed producer authority

This component is a development implementation, pending independent assessment
and integration tests. It does not authorize publication or arbitrary cleanup.

Only a **new, exclusively created APS store** is supported. Existing files,
histories, worktrees and roots cannot be adopted or imported. The broker creates
flat regular artifacts with opaque, never reused IDs. Producers receive IDs and
bounded byte copies, never writable filesystem paths or kernel descriptors.
Each artifact is at most 512 KiB; a store permits 1,024 artifact registrations
and tasks. These initial bounds are deliberate refusals, not silent truncation.
Rename, hardlink, import and producer cleanup routes do not exist.

All participating producer reads, creates, references, leases, lifecycle changes
and resumes use `ProducerClient` and the same live `AuthorityServer`. A local
SQLite registry owns generations, object registration, claims and retirement.
Native AO/Kynlo observations are additional evidence only. They do not fence
native resumes or unrelated filesystem users and never grant deletion.

## Trust boundary

This is a cooperative closed-store protocol, **not an OS sandbox**. Mode 0700
separates other principals, not hostile processes with the same UID. Raw same-UID
access, outside-protocol namespace movement, external hardlinks and mount actors
are outside the supported enforcement contract. Python stat followed by unlink
is not inode-conditional deletion or atomic ancestor containment. Unknown
identities and unexpected store entries refuse operations. Unmanaged data stays
KEEP and is never imported merely because its owner, PID or manifest matches.

## Owner and producer roles

`ManagedAuthority.create(path)` returns the trusted owner object. Its opaque
`owner_key` must be saved only to an explicitly chosen, newly created 0600 owner
key file by the launcher. Never put it in argv, environment, stdout or reports.
An immutable private bootstrap authenticates before SQLite recovery can write.
`ManagedAuthority(path, owner_key=key)` opens that owner role.

`AuthorityServer(authority, socket_path=None, collector=None)` exposes a local
Unix socket. Context entry starts its serving thread; `serve_forever()` is the
blocking form; `close()` stops only that server and removes only its own socket
identity. An occupied endpoint or endpoint longer than 100 bytes is refused.
The optional collector is owner-side, never a producer RPC route.

`ProducerClient.register(socket_path, owner_key, task_id, producer_id,
grace_seconds=1)` is a trusted launcher operation, returning a producer client.
Ordinary producers retain only their bounded task/generation token.
`write`, `read`, `reference`, `lease`, `release`, `set_state` and `terminal` use
the broker. A launcher-authorized `resume(owner_key)` returns a fresh client and
revokes old generations. Terminal transitions do not clear references or leases.
Even an expired lease retains an object until explicit fenced release. A fresh
owner-resumed producer may release its own previous-generation claims.

The registry stores content digests and bounded outcomes, never artifact READ
bytes or WRITE bodies. Read replay reauthorizes and reads the still-live object;
it cannot retrieve a retired object's payload from a persistent response cache.
Owner register/resume request keys bind their complete payload and replay the
original generation without rotation. A domain-separated HMAC derives the same
producer capability from the authenticated owner secret, store, request and
generation; only capability hashes and bounded non-secret receipts persist.
Control format 2 refuses earlier development stores rather than silently
retaining or migrating their former payload cache. The socket protocol stays
version 1. Unknown old stores remain retained for explicit owner inspection.
Unknown client delivery blocks automatic replay; no endpoint is guessed.

## Fence and durable retirement

Every operation first acquires a process-wide mutex keyed by physical store
identity, then a fresh anchored flock descriptor. Both remain held across all
SQLite commits, the collector effect and the outcome decision. Sharing a flock
descriptor is not thread exclusion. Inherited authority/server objects refuse
use after fork; children do not unlock an inherited parent fence. A child may
construct a fresh object with fresh process-local mutexes and lock descriptors.

The monotonic deadline bounds mutex/flock/DB acquisition and permission for new
effects. It cannot bound every kernel I/O/flush duration. SQLite connections
configure and verify DELETE journaling, FULL synchronization and fullfsync.
Filesystem flushes and SQLite commits remain separate obligations. Tests do not
certify hardware power-loss durability.

Owner `snapshot(task_id)` takes the same fence. Its DTO contains store and
authority-domain epoch, revision, task/generation/status/grace, root identity,
selected task `artifacts`, and claims from **all holders** for those objects.
`inventory` lists every registered artifact across tasks; `registered_names`
lists all registered basenames, including permanently retired IDs. Artifact
`task_id` is its owner; claim `task_id` is its holder. Names are UUID hex plus
`.blob`; identities contain dev, ino, uid, size, mtime_ns and ctime_ns.

Owner `approve(task_id, plan_digest, artifact_ids, expected_revision,
operation_id, ttl_seconds)` binds store/task/generation/revision, exact object
set, plan and operation key. Approval does not bump eligibility revision.
Duplicate approval cannot extend its expiry or change its binding.

`transaction(task_id, expected_revision, deadline)` yields `Fence` with a
borrowed `root_fd` and fresh snapshot. `expected_revision=None` permits initial
fenced inspection, **never prepare**. `prepare(operation_id, plan_digest,
artifact_id, expected_identity, approval_id)` rechecks terminal/grace, claims,
disposable class, origin, identity, deadline and grant. It durably commits a
RETIRING tombstone, generation revocation and complete intent before returning
an internal `PreparedIntent`. The collector must check that token immediately
before any effect. Its own cleanup epoch remains valid across its transitions.

The collector owns no-follow filesystem and platform exclusion checks, actual
unlink and namespace synchronization. `finish(intent, outcome, receipt)` accepts
only `unlinked`, `not_removed` or `unknown`, binding the full **pre-effect**
identity. Optional post-unlink identity uses stable dev/ino with nlink zero,
not the old ctime. Finish may record an already-performed effect after deadline;
it never grants a new one. Verified logical removed bytes require a durable
confirmed unlink receipt. Reclaimed capacity remains unknown.
Unknown caller receipt fields, including nested post-unlink extras, are dropped
before persistence or return. Receipts contain only the documented metadata DTO.

Pending/unknown intent blocks ALL producer grants, reads, writes, references,
leases and resumes after crash-released locks. A trusted explicit
`reconcile(operation_id, artifact_id)` observes identity without deleting:
missing becomes `reconciled_missing` with unknown causality/removed bytes;
the same object present becomes permanently retired and retained; replacement
or unknown identity remains frozen. IDs are never resurrected or reused.
Trusted `pending_operations(task_id=None)` and snapshot `pending_operations`
expose exact operation_id, artifact_id, task_id, plan_digest, kind and
status/outcome for recovery discovery, including while the gate is active.
Producer-create operation IDs use a bounded domain-separated SHA-256 form;
the maximum accepted 128-character request ID remains publicly reconcilable.
SQLite configuration failures close the allocated connection. Server shutdown
refuses inherited use, returns without waiting on an unstarted server, and
bounds waiting on its own serving loop. Kernel I/O itself is not universally
time-bounded.

`operation_receipts` validates exact operation/plan/approval/object binding before
stale revision checks. Completed requests return stored receipts only. Changed
payload refuses; pending requires recovery; partial batches require a fresh
review rather than replaying unstarted deletions. `prepare` returning a receipt
dictionary is likewise a completed replay, never a fresh effect token.
The `producer:` and `owner:` operation domains are reserved and cannot be
approved as collection requests. Replay additionally checks retirement kind,
store/task, exact plan/approval/object set and stored receipt identity/outcome;
a producer-create receipt can never masquerade as completed collection.
Fence checks repeat their deadline test after their own identity/DB observations.

## Research basis

## Read-only managed accounting

Trusted owner `accounting_snapshot(task_id)` returns factual DTO schema
`agent-pooper-scooper/managed-accounting`, version 1. One existing authority lock
covers selected-task registry facts, durable creation/retirement records and
anchored no-follow metadata observations. It works during recovery, performs no
commit, grants no deletion and has no producer RPC route. Cleanup primitives
and collector approval/fencing semantics are unchanged.

Artifacts expose only the agreed accounting whitelist: opaque ID, class and
policy, lifecycle flags/generation/time, nullable six-field identity, verified
creation size/allocation, current verification status/nullable size/allocation,
and counts of all reference/lease holders including expired claims. Historical
creation bytes require a matching durable completed-create record plus final
registration identity and origin. Pending/unmaterialized/reconciled incomplete
creates remain unverified with null historical bytes. A current missing or
changed file never supplies invented current size.

Retirement outcomes check selected task/store/object, exact operation/plan/
approval/identity bindings and canonical confirmed unlink bytes. Producer-create
records are not retirement. Pending, unknown and reconciled-missing causality
never become verified removed bytes. Audit entries are bounded derived views of
existing records, not new writes; unavailable persisted event times stay null.
Safe fixed-code unknowns and `complete` qualify downstream lower-bound totals.

The view is bounded to 1,024 artifacts and 2,048 outcomes/audit entries, with
bounded metadata and unknowns; excess refuses instead of truncating silently.
It exports no filesystem paths/names, payload, capability/key, socket or native
provider identifiers. Reporting code owns totals and public projections. This
read API itself is neither a cleanup action nor a task-end/native callback.

## Research basis

The independent research pass inspected containerd v2.4.2
[`b7a25261`](https://github.com/containerd/containerd/blob/b7a25261e029d68978bbf290df0a62fc819b3c51/core/metadata/db.go)
and BuildKit v0.34.1
[`f5b59a1f`](https://github.com/moby/buildkit/blob/f5b59a1f2fb25a0cb7072c398cab9aed7835da4e/cache/manager.go):
owned registries, explicit leases, stable lock ownership and logical retirement.
Both upstream licenses were inspected as Apache-2.0; their Go/runtime dependency
stacks were not adopted. No code was copied. In particular, APS does not borrow
automatic lease expiry. Public advisories and limitations were reviewed without
claiming exhaustive transitive auditing or executing upstream tests.

The implementation instead uses existing stdlib SQLite/fcntl/threading/socket
interfaces. [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[synchronous/fullfsync](https://www.sqlite.org/pragma.html#pragma_fullfsync) and
[Apple flock semantics](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html)
inform the durability and coordination contract. SQLite is public domain;
project MIT licensing and runtime dependency declarations are unchanged.
