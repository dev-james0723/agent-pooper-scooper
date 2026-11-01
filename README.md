# 🧹 Agent Pooper Scooper

**Your agents make a mess. We scoop it.**

*Save your disk from the waste your AI coding agents leave behind.*

Agent Pooper Scooper is an open-source project for **task-aware, safety-first disk cleanup and friendly Scoop Reports**. The goal: reclaim disposable scratch space without destroying work that AI coding agents, other tasks, or future resumed sessions might still need.

> **Project status: pre-alpha / design stage.** This repository contains the public product plan and safety specification, **not a working cleanup engine**. No automatic cleaner has been released or installed by this project.

## Why?

Coding agents can generate gigabytes of build outputs, test caches, temporary files, logs, and artifacts. Traditional cleaners don't know whether a file is still needed by another worker or a resumable session.

**Task completed does NOT mean its files are disposable.**

## Planned features

- **Task-aware attribution** — know what each agent/task created and how much disk space it used.
- **Safe cleanup** — auto-remove only explicitly disposable, task-owned scratch files after checked leases, reference checks and a retention grace period.
- **Scoop Reports** — after every task, report created, retained, cleanup-eligible, removed and *actually reclaimed* bytes, with categories, receipts and safety skips.
- **Disk pressure alerts** — watch available space and warn before your SSD fills up.
- **Agent integrations** — provider-neutral task event API, with future optional orchestrator/desktop-control adapters.
- **Auditability** — dry-run, reproducible decisions, detailed reasons, rollback/quarantine where practical, and independent security tests.

These are **planned capabilities**, not shipped features.

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
2. Automatic deletion is limited to deliberately registered, task-owned disposable scratch roots, with strict policy checks.
3. Never automatically delete uncommitted code, worktrees, drafts, session histories, local models, original media, user downloads, credentials or unknown files.
4. Recheck permissions and file identity immediately before changes; defend against symlink traversal and filesystem races.
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
