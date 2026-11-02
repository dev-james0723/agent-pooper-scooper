# Roadmap

The legacy Phase 1/2 flow remains read-only. Preview 0.1.0a1 adds direct workstation entry points over the existing closed-store core. Checked items describe implemented behavior; acceptance of global native cleanup is a separate outstanding goal.

- [x] Define versioned provider-neutral task event and artifact manifest schemas with strict validation
- [x] Package a Python 3.10+ stdlib-runtime CLI with manifest init/record/validate/show
- [x] Implement on-demand read-only volume telemetry and bounded registered-root attribution
- [x] Generate JSON/Markdown Scoop Reports with public path redaction and explicit unknown accounting
- [x] Test hardlink deduplication, symlinks, permissions, identity mismatch, traversal limits and simulated mount boundaries in tempfile fixtures
- [x] Add a read-only regular-file planner with strict snapshot evidence, KEEP reasons, grace, reference/lease claims and stale-plan invalidation
- [x] Add null/file adapter seams and manually triggered task-end reports; execution always blocked
- [x] Add bounded native usage reports and lightweight disk-pressure checks
- [x] Add a scheduled macOS monitor with private state, six-hour inventory refresh and opt-in pressure alerts
- [x] Add live APS-managed producer lifecycle, references/leases and fenced generation/resume protection for newly created broker objects
- [x] Default managed collection to dry-run, with strict plans, identity checks and fixture-based negative tests
- [x] Collect explicitly approved flat managed files with durable intent/finish, crash recovery, tombstones, partial receipts and zero-count replay
- [x] Verify real installed producer/collector flows, thread/process fences and a device-bound synthetic command gateway fixture; no existing data adoption
- [x] Add a foreground batch-command runner with automatic completion reports and explicitly scoped successful-output collection
- [ ] Enroll real native agent scratch producers and automate their task-end cleanup; command-output capture does not supply this authority
- [x] Test cooperating concurrent producers and hardlink/symlink/identity/mount-unknown refusal in temporary fixtures
- [ ] Establish APFS clone/snapshot/open-file reclamation, hostile namespace/mount containment or hardware power-loss guarantees (currently unsupported)
- [ ] Complete exact final integrated authority/executor/adapter review, runtime checks and hosted supported-version/platform gates
- [ ] Publish and install each preview only after exact-commit independent review, tests and publication gates

No private internal engineering repository, task transcript, account token or machine-specific configuration should be copied into this public project.

## Preview acceptance

The preview must separately verify package installation, real native inventory,
the scheduled monitor, default output retention, failed-command retention,
explicit successful-output collection and preservation of command-created
source/session files. A Git push alone satisfies none of these runtime checks.

Historical caches, resumable worktrees and other unregistered native data stay
outside execution. Claims about net reclaimed APFS capacity, host-wide cleanup
and automatic enrollment remain unsupported.
