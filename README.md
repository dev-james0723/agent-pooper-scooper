# 🧹 Agent Pooper Scooper

**Your agents make a mess. We scoop it.**

*Save your disk from the waste your AI coding agents leave behind.*

Agent Pooper Scooper is an open-source project for **task-aware, safety-first disk cleanup and friendly Scoop Reports**. The goal: reclaim disposable scratch space without destroying work that AI coding agents, other tasks, or future resumed sessions might still need.

> **Project status: pre-alpha / managed executor development.** Legacy manifests, telemetry, reports and file/null evidence planning remain read-only, with their execution BLOCKED. The experimental `aps managed` commands can collect explicitly approved regular files created by the APS producer broker in a new closed store. They cannot adopt existing user data or provide host-wide cleanup. Independent authority/executor/integration acceptance remains required; background cleaning and global native lifecycle authority are unavailable.

## Why?

Coding agents can generate gigabytes of build outputs, test caches, temporary files, logs, and artifacts. Traditional cleaners don't know whether a file is still needed by another worker or a resumable session.

**Task completed does NOT mean its files are disposable.**

## Features and roadmap

- **Manifest-based attribution** — record artifacts with task, host, producer, class, explicit disposable flag, timestamps, and device/inode identity. Unrecorded writes are not attributed.
- **Managed collection** — explicitly approve a fresh plan for broker-created disposable regular files, with terminal/grace checks, unreleased-reference/lease protection, durable intent and receipt accounting. Dry-run is the default; existing directories cannot be adopted.
- **Scoop Reports** — legacy reports distinguish measured, retained, candidate, unknown and skipped accounting with zero removal. Managed collection separately reports durable verified unlink bytes and replay/recovery outcomes; reclaimed capacity remains unknown.
- **Read-only telemetry** — volume capacity via `statvfs`, bounded registered-root walks, hardlink deduplication, symlink refusal, mount-boundary skips, and explicit unknowns.
- **Provider-neutral events** — versioned strict schemas and JSON Lines validation. Automatic integrations remain future work.
- **Managed producer lifecycle** — opaque object IDs and bytes, explicit reference/lease release, revoked generation tokens on terminal state, and owner-controlled resume. Every participating operation uses the same broker fence.
- **Future capabilities** — background pressure alerts, broader task-end automation and optional independently verified provider transports. Native-global reference/resume authority is unavailable.

A disposable flag or terminal event never grants deletion authority. See [managed execution safety](docs/EXECUTION_SAFETY.md) for the separate closed-store owner approval, producer fence, recovery protocol and unsupported containment assumptions. No existing manifest or native snapshot can authorize that executor.

## Install and try Phase 1

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
