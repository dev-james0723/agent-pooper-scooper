# 🧹 Agent Pooper Scooper

**Your agents make a mess. We scoop it.**

*Save your disk from the waste your AI coding agents leave behind.*

Agent Pooper Scooper is an open-source project for **task-aware, safety-first disk cleanup and friendly Scoop Reports**. The goal: reclaim disposable scratch space without destroying work that AI coding agents, other tasks, or future resumed sessions might still need.

> **Preview 0.2.0a1: safe local autopilot and a native macOS menu bar.** Observe recognizable agent processes and automatically collect new, explicitly disposable APS-captured diagnostics after successful completion and 24-hour retention. Pause or hold cleanup, inspect exact receipts, and optionally add advisory native-agent startup guidance. Existing native files, worktrees, caches and session histories remain outside cleanup. This is not a heuristic cleaner for historical agent data.

## Start here

The preview needs Python **3.10+**. Its runtime uses only the standard library.
Install a verified release wheel in a virtual environment, or use the
[macOS installer](docs/INSTALL.md) to add the `aps` command, a desktop report
shortcut and a low-overhead login monitor. No orchestrator is needed.

```sh
aps host scan --format markdown
aps host pressure --format markdown

# The parent must already exist and be owner-controlled. Each run directory is new.
aps run --run-dir "$PWD/new-diagnostic-run" --clean-success -- python3 -m unittest discover
```

`aps host scan` reports selected native locations, their measured sizes,
incomplete measurements and disk availability. It keeps every existing file.
The macOS monitor checks capacity every 15 minutes and refreshes its bounded
inventory every six hours; low-space desktop alerts have a six-hour cooldown.

`aps run` is for noninteractive diagnostic, test or build commands. It streams
and captures at most 16 MiB of newly produced stdout/stderr into its own store.
Without `--clean-success`, captured output stays retained. With that option,
only completely captured output from a successful command is eligible for the
existing exact-plan collector. Failure, timeout, interruption, a capture limit
or uncertainty keeps the data. The command's own files are never cleaned.
Each run keeps a private `report.json`, owner capability and audit records;
confirmed removed blob bytes do not imply net disk savings or reclaimed APFS
capacity. Small runs can retain more metadata than the output they remove.

See [native reports](docs/HOST_REPORTS.md), [the task runner](docs/TASK_RUNNER.md),
and [installation and monitoring](docs/INSTALL.md). Do not wrap an entire
coding-agent session with disposable-output cleanup: its output may be valuable.

## Automatic cleanup

See [autopilot setup and bounds](docs/AUTOPILOT.md), [native menu-bar setup](docs/MENUBAR.md) and
[advisory agent startup integration](docs/AGENT_HOOKS.md). The owner-enabled scheduler
runs every 60 seconds; cleanup applies only to new enrolled disposable output, not
all files touched by detected agents. This preview retains audit records and caps
a policy at 128 lifetime enrollments; it refuses further enrollment rather than
silently deleting history.

## Why?

Coding agents can generate gigabytes of build outputs, test caches, temporary files, logs, and artifacts. Traditional cleaners don't know whether a file is still needed by another worker or a resumable session.

**Task completed does NOT mean its files are disposable.**

## Features and roadmap

- **Manifest-based attribution** — record artifacts with task, host, producer, class, explicit disposable flag, timestamps, and device/inode identity. Unrecorded writes are not attributed.
- **Managed collection** — explicitly approve a fresh plan for broker-created disposable regular files, with terminal/grace checks, unreleased-reference/lease protection, durable intent and receipt accounting. Dry-run is the default; existing directories cannot be adopted.
- **Scoop Reports** — legacy reports distinguish measured, retained, candidate, unknown and skipped accounting with zero removal. Managed collection separately reports durable verified unlink bytes and replay/recovery outcomes; reclaimed capacity remains unknown.
- **Read-only telemetry** — volume capacity via `statvfs`, bounded registered-root walks, hardlink deduplication, symlink refusal, mount-boundary skips, and explicit unknowns.
- **Provider-neutral events** — versioned strict schemas and JSON Lines validation. Native session enrollment remains future work.
- **Managed producer lifecycle** — opaque object IDs and bytes, explicit reference/lease release, revoked generation tokens on terminal state, and owner-controlled resume. Every participating operation uses the same broker fence.
- **Workstation entry points** — bounded native inventory, a scheduled pressure monitor, and a direct command-output runner with private receipts.
- **Future capabilities** — broader task-end automation and native producer integrations. Native-global reference/resume authority is unavailable.

A disposable flag or terminal event never grants deletion authority. See [managed execution safety](docs/EXECUTION_SAFETY.md) for the separate closed-store owner approval, producer fence, recovery protocol and unsupported containment assumptions. No existing manifest or native snapshot can authorize that executor.

## Advanced manifest-based reporting

Requires Python **3.10+** on macOS/POSIX. Runtime dependencies are entirely standard library. Builds use setuptools 58+ and wheel; provision approved build tools first for offline installation:

```sh
python3 -m pip install --no-index --no-deps --no-build-isolation .
aps --version
aps manifest init task.json --task-id demo-1 --producer example-agent \
  --host example-host --root /absolute/physical/task-scratch
aps manifest record task.json /absolute/physical/task-scratch/build \
  --class build-output --disposable
aps manifest validate task.json
aps telemetry task.json --public
aps report task.json --public --format markdown
```

The root and artifact must already exist. Use physical paths without symlink ancestors; on macOS `/tmp` and `/var` may be aliases. Without task events, status is unknown and all measured artifacts are retained. `--public` redacts registered-root prefixes, home/personal path prefixes, and host; inspect relative filenames before sharing.

See [Phase 1 usage and limitations](docs/PHASE1.md) for events, offline packaging verification, and remaining gates.

## Read-only planning and task-end reports

```sh
aps plan task.json                          # no evidence: KEEP
aps schema planner-evidence                 # strict operator snapshot contract
aps plan task.json --evidence snapshot.json --public
aps task-end task.json --events events.jsonl --evidence snapshot.json --public
```

File evidence is an unauthenticated operator assertion, bound to the manifest digest and a short freshness window. The planner checks explicit root approvals, identity, terminal state, grace, complete reference/lease claims, Git/worktree and mount exclusions. Unknown means KEEP. Candidates are regular files only; even a `WOULD_REMOVE` result has execution **BLOCKED**. `--execute` returns exit 1 without performing cleanup. See [planner boundaries and research](docs/PHASE2.md).

## Cooperating producer lifecycle (opt-in)

The lifecycle SDK can start an explicitly enrolled cooperating producer and
automatically record its authenticated normal completion and qualified report:

```python
# The trusted host supplies an authenticated owner, serving broker and private journal.
from agent_pooper_scooper.managed_lifecycle import ManagedTaskCoordinator

coordinator = ManagedTaskCoordinator(owner, broker.socket_path, journal_path)
client = coordinator.enroll("example", "cooperating-client", "enroll-example")

def task(producer):
    generated_result = bytes(range(17))  # Illustrative primary task bytes, not an imported file.
    producer.write(generated_result, disposable=True)

result = coordinator.run_producer(client, task, "run-example")
```

`aps managed lifecycle init` explicitly creates a new private journal;
`enroll`, `resume`, `task-end` and `status` only open a validated existing one.
Credential/report files are exclusive 0600 outputs outside the store and journal.
Default task-end processing evaluates and reports; later collection requires
an exact fresh owner grant and explicit `--execute`. Delivery additionally
requires `--send` and a configured private callback. Replays cannot re-run the
producer or create another delivery slot; unknown stages stop for inspection.

This is a cooperative protocol, not a Python sandbox or automatic native
AO/Kynlo enrollment. Producer artifacts must be broker-owned from creation;
copying an unmanaged input does not reclaim its original. Existing user/native
data stays outside execution. See [lifecycle setup, bounds and remaining
acceptance gates](docs/MANAGED_LIFECYCLE.md). The SDK alone installs no service;
the separate macOS installer can enable the read-only disk monitor.

## Unified managed task reports

After a producer records terminal state and the owner evaluates or collects its managed files:

```sh
aps managed report /physical/aps-store --owner-key-file /physical/owner.json \
  --task-id configured-task --public --output /physical/new-scoop-report.json
```

This command only reads one fenced owner accounting view. It joins durable broker-attributed creation facts, current retained files and reasons, cumulative confirmed removal receipts, unknowns and audit facts. Unknown history stays null with qualified known lower bounds; missing objects are not invented removals. Cumulative report totals are separate from the collector's per-invocation accounting and zero-new-removal replay. Reclaimed capacity remains unknown. The whole report is compatible with explicit numeric-only notification delivery. Legacy `report`/`task-end` keep their read-only manifest semantics. See [the report contract](docs/SCOOP_REPORT.md).

## Explicit live observations and report delivery

```sh
aps integrations observe-ao --session-id configured-session --project-id configured-project --public
aps integrations observe-kynlo --device-id configured-device --mission-id configured-mission --agent --public
```

AO observations invoke and parse the native CLI but never grant cleanup authority. Kynlo requires an explicit verified device/mission/agent-handle binding and a configured callback; unavailable handles/transports remain unknown. No endpoint, credentials, default device or global reference/resume fence is inferred.

After managed collection exits its fence, `aps integrations notice` can project a selected report into a numeric-only notification. It defaults to `not_attempted`; `--send` requires an explicit trusted argv bridge or private supervised stdio transport. The owner capability stays in its private file. The persisted intent prevents blind resending on timeout or restart. `notice-status` reads/reconciles delivery classification; provider acknowledgement never proves someone saw a banner. Private stdio frames contain routing identifiers and cannot be combined with `--public`. See [the transport and delivery contract](docs/LIVE_INTEGRATIONS.md).

## Example Scoop Report

Illustrative numbers only — no cleanup has occurred:

| Metric | Example |
| --- | ---: |
| Task-generated files | 4.8 GB |
| Retained: source/drafts/artifacts | 1.7 GB |
| Eligible task-owned scratch/build files | 3.1 GB |
| Files actually removed | Not measured |
| Verified disk capacity reclaimed | Not measured |

A real report must distinguish file sizes from actual reclaimed SSD capacity (which APFS snapshots, clones, hard links, Trash and concurrent writes can affect).

## Safety contract

1. **Observe-only by default.** Unknown owner, file identity, task state, or lease means **keep**.
2. Executable collection is limited to files created by the APS broker in a new closed store, with a reviewed plan and explicit owner approval. Existing registered roots remain read-only.
3. Never automatically delete uncommitted code, worktrees, drafts, session histories, local models, original media, user downloads, credentials or unknown files.
4. Recheck permissions, no-follow identity, deadlines and mount exclusions inside the shared producer fence. Raw same-UID writers and external namespace/mount actors are outside the managed contract; metadata checks are not atomic containment.
5. Task-completed events trigger **evaluation**, never blanket deletion.
6. Report skipped files and uncertainties; never invent storage savings.

Read the [Safety Model](docs/SAFETY_MODEL.md), [Roadmap](docs/ROADMAP.md) and [Scoop Report specification](docs/SCOOP_REPORT.md).

## Proposed architecture

Agent events → Task manifest → Ownership and lease verification → Dry-run policy → Allowlisted cleanup → Verified Scoop Report → Notification.

Initial target is macOS, with portability and optional integrations as future work. No proprietary code or personal workstation configuration is required to develop the core project.

## Contributing

Security reviews and tests that prove what **must not be deleted** are especially valuable. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).

## AO contributions and review

This product can be developed by an Agent Orchestrator team, but only files from this dedicated public repository may be published. See [the AO-to-GitHub workflow](docs/AO_SYNC.md) for prepublication safeguards, local dry-run and draft pull requests. No private orchestrator history is synchronized.

## Safe autopilot preview (0.2.0a1)

Owner-enabled periodic cleanup is available for **new explicitly disposable APS
wrapper capture** after successful completion and a default 24-hour retention.
Automatic same-UID agent discovery is read-only and does not enroll native
sessions or files. See [autopilot controls, installation and limits](docs/AUTOPILOT.md).
