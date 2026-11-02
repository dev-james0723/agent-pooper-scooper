# Managed execution safety

This development executor supports one boundary: a **new APS-created flat artifact store whose participating producers use the APS broker for every read, write, reference, lease, state change and resume**. It cannot adopt existing directories, manifests, user files, AO histories, worktrees or caches. The legacy manifest/evidence planner remains read-only and its execution remains BLOCKED. Null/file evidence and native provider status are never deletion authority.

This protocol is not an operating-system sandbox. A hostile same-UID process, exported or inherited access, raw filesystem writers, and namespace or mount changes outside the protocol are unsupported. Mode 0700 separates other principals; it does not constrain the same UID. Descriptor-relative stat followed by unlink is **not** an inode-conditional atomic delete, and an opened directory is not proof against an external ancestor move. Do not use this executor where those excluded actors can operate. Native mount observations supply exclusions, not atomic containment.

## Owner and producer roles

`aps managed init` creates a new physical directory, private control database and stable lock, and an owner capability in a new 0600 file. Existing paths are refused. Store initialization and capability-file persistence are separate effects: failure can leave an incomplete private fixture for inspection; the CLI never adopts it or silently erases it. Owner capabilities are not arguments, environment variables, printed values, or provider-discovered credentials. Protect the capability file as a secret.

Only the trusted owner can approve, collect or reconcile. A producer launcher may register or resume using an owner-capability file, storing the resulting restricted producer capability in a separate new 0600 file. Ordinary producers receive only their restricted capability. The Unix socket exposes producer operations, never approval or cleanup. Producers exchange bytes and opaque IDs, not writable file descriptors or managed file paths. Rename, hardlink, import and adoption are unsupported. Producer-create operation keys use a reserved domain that collection refuses.

The producer CLI requires a stable request ID for mutation. Preserve that ID and exact payload when investigating uncertain delivery; never generate a new key and blindly repeat a write or generation transition. READ copies selected bytes into a new private file and prints only a bounded receipt. No file contents, owner keys or producer tokens belong in collection receipts or public output.

Example owner flow, using newly created synthetic fixtures only:

```sh
aps managed init /physical/new-store --owner-key-output /physical/new-owner.json
aps managed serve /physical/new-store --owner-key-file /physical/new-owner.json --socket /physical/short.sock
# In another process, a trusted launcher registers its producer with an owner-key FILE.
aps managed producer --socket /physical/short.sock register \
  --owner-key-file /physical/new-owner.json --task-id demo --producer-id example \
  --credentials-output /physical/new-producer.json --request-id register-demo
aps managed producer --socket /physical/short.sock --credentials-file /physical/new-producer.json \
  start --request-id start-demo
# Write synthetic bytes via --input and explicitly select --disposable when appropriate.
# Record terminal state and allow the configured grace to elapse before owner planning.
aps managed plan /physical/new-store --owner-key-file /physical/new-owner.json \
  --task-id demo --output /physical/new-plan.json
aps managed collect /physical/new-store --owner-key-file /physical/new-owner.json --plan /physical/new-plan.json
aps managed approve /physical/new-store --owner-key-file /physical/new-owner.json \
  --plan /physical/new-plan.json --operation-id collect-demo --output /physical/new-approval.json
# Inspect the exact local plan and grant. Only then explicitly select execute,
# supplying the returned approval ID and the SAME operation ID.
```

Commands require physical no-follow paths; macOS `/tmp` and `/var` aliases are not accepted as physical ancestors. Unix socket paths must fit the platform bound. The default is dry-run. A public preview omits private binding identifiers and is not an executable plan. Existing output files, linked ancestors/leaves and malformed or permissive capability files are refused. Collection reserves a new output before invoking effects, so a known output refusal cannot occur after artifact deletion. A later write/flush/pipe failure is still a possible post-effect ambiguity; inspect durable owner receipts before retrying.

## Fence and eligibility

A process-wide mutex keyed by physical store identity plus a freshly opened advisory lock serializes all owner and producer operations. Both remain held across SQLite commits, collection effects and durable finish. A shared descriptor's `flock` is not a thread mutex. Inherited authority/fence/server objects are refused after fork; a fresh child must acquire its own store operation. Lock acquisition, database busy acquisition and permission to begin effects have monotonic deadlines. Kernel filesystem and flush calls themselves have no universal wall-clock deadline.

Collection requires the exact reviewed plan, current registry revision, store/task/generation/identity binding, and an owner approval bound to the exact object set, plan digest, operation key and expiry. Rebuilding the plan under the fence checks all registered inventory and fresh mount/root/file observations. Tampering or stale state refuses. Batches are bounded to 64 selected objects and directory inventory to 1024 entries. There is no recursive artifact removal.

Only broker-created, registered, single-link regular files with private permissions, matching owner/device/inode/content metadata, explicit disposable class and origin can qualify. All unknown entries, special files, symlinks, Git markers, broad or unverified roots, changed identities and unsupported mount observations refuse. Protected classes and non-disposable objects retain. Active/paused/resumable state, unknown/future clocks, insufficient terminal/write grace and **every unreleased reference or lease, including an expired lease**, retain. Terminal state revokes the producer generation without silently clearing claims. A newly owner-resumed generation can release its holder's old claims; retired object IDs are never reused or resurrected.

## Effect ordering and recovery

For each object, while holding the same fence:

1. Open the registered file no-follow/nonblocking, verify the exact regular-file identity and root/native mount observations.
2. Commit and flush a durable intent, RETIRING/tombstone and producer-generation revocation **before unlink**. The cleanup epoch accepts its own transitions while stale producer tokens fail.
3. Recheck the effect deadline, original named identity and namespace binding, unlink only that registered basename relative to the held root descriptor, and verify namespace absence and held object identity/link count.
4. Synchronize the directory namespace, then durably finish the identity-bound receipt before releasing the fence. Finish may record an already-applied effect after the deadline; the deadline never authorizes a new unlink.

A prepared intent, unknown result or failed durable finish persists a recovery gate. All new producer effects/resume and new collection refuse until explicit trusted reconciliation. A crash releasing the OS lock is not evidence that the task cannot resume. `aps managed status` discovers pending reconciliation keys through the owner API; `reconcile` observes the identity and never blindly repeats deletion or edits the database outside the protocol.

Missing after an ambiguous crash becomes `reconciled_missing` with causality unknown and removed bytes unknown, not an invented successful unlink. The same object still present becomes permanently retired/retained. A replaced or unverified object stays frozen. A never-created object recovered as missing keeps its ID/tombstone without inventing device/inode/byte identity; it is always KEEP and cannot poison a later valid producer plan. Physical inode uniqueness is checked for current objects, not the lifetime history of absent tombstones. Same operation key/payload returns stored completed receipts with **zero new removals**; changed payload refuses. Partial batches report only durable confirmed effects. They make no all-or-nothing promise and do not automatically replay a remainder.

SQLite FULL and namespace flushing are separate. Process-crash tests do not prove power-loss durability of APFS, storage controllers or hardware caches. Flush/configuration failures retain or require recovery. The stable lock inode and control database are outside the deletable object set and are never collection targets.

## Accounting and public output

Candidate apparent bytes, allocated bytes and filesystem capacity are distinct. Removed counts/bytes include only this invocation's durable confirmed unlink receipts. An unlink followed by an ambiguous or failed finish contributes no verified removed bytes in that invocation. Replays return prior receipts separately without double counting. Reconciled missing objects have unknown causality. Actual reclaimed capacity is always unknown (`null`): snapshots, clones, open descriptors and concurrent activity prevent task-specific reclamation claims.

Public managed output omits store/task/object IDs, roots, socket names, plan hashes, capabilities and private receipt bindings. Public display objects cannot be used for execution. CLI errors are generic even outside public mode so unexpected input/transport exceptions cannot print capability contents or traceback paths. Private operator files must not be published.

## Unified read-only task accounting

After terminal state or collection, `aps managed report` joins one owner-fenced accounting view with verified historical creation, current retention, cumulative confirmed removal receipts and bounded unknown/audit facts. It works for recovery inspection and has no mutation/notification role. It never trusts caller-supplied receipts or turns missing files into successful unlinks. Unknown totals remain null with explicit known lower bounds; per-file allocated observations are not exclusive capacity. Collector invocation/replay accounting is unchanged. See [the full report contract](SCOOP_REPORT.md) before delivering a selected report through an explicitly configured notifier.

## Research and limits

The design adapts coordination ideas, not source, from [containerd v2.4.2 metadata/GC locks](https://github.com/containerd/containerd/blob/b7a25261e029d68978bbf290df0a62fc819b3c51/core/metadata/db.go) (2026-10-13, Apache-2.0) and [BuildKit v0.34.1 serialized prune](https://github.com/moby/buildkit/blob/f5b59a1f2fb25a0cb7072c398cab9aed7835da4e/cache/manager.go) (commit 2026-10-07, Apache-2.0). Their Go/container/snapshot dependencies are not adopted. Prior planner research remains in [PHASE2.md](PHASE2.md). The runtime uses only existing Python standard-library modules; no third-party runtime dependency or upstream implementation was copied.

Primary interface evidence: [Python unlink](https://docs.python.org/3.10/library/os.html#os.unlink), [Apple flock descriptor semantics](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html), [SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous) and [fullfsync](https://www.sqlite.org/pragma.html#pragma_fullfsync), [Darwin statfs ABI](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/sys/mount.h), and [Linux mountinfo](https://docs.kernel.org/filesystems/proc.html#proc-pid-mountinfo-information-about-mounts). Darwin observations currently require the known 64-bit ABI and local writable APFS; Intel selects the explicit INODE64 symbols. Linux supports observed writable ext4/xfs/btrfs only. Unknown interfaces, malformed tables, unsupported filesystems and ambiguous topology refuse. Hosted Linux validation is a separate acceptance gate from local macOS tests.

Development tests use only temporary newly created stores and generated synthetic bytes. Actual fixture unlinks, same-process producer waiters across prepare/finish commits, a separate-process fresh-lock canary, crash boundaries, partial accounting and installed CLI flows are required evidence; passing them is not proof of unsupported hostile containment, hardware power loss or global native AO/Kynlo fencing. Native lifecycle observations and notification transports remain separate product integrations with their own evidence and delivery classifications. They cannot manufacture a managed deletion capability.
