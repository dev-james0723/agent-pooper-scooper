# Roadmap

The legacy Phase 1/2 flow remains read-only. Closed-store collection is a separate pre-alpha scope. Checked items describe implemented and locally verified behavior, not a release or acceptance of unsupported global cleanup.

- [x] Define versioned provider-neutral task event and artifact manifest schemas with strict validation
- [x] Package a Python 3.10+ stdlib-runtime CLI with manifest init/record/validate/show
- [x] Implement on-demand read-only volume telemetry and bounded registered-root attribution
- [x] Generate JSON/Markdown Scoop Reports with public path redaction and explicit unknown accounting
- [x] Test hardlink deduplication, symlinks, permissions, identity mismatch, traversal limits and simulated mount boundaries in tempfile fixtures
- [x] Add a read-only regular-file planner with strict snapshot evidence, KEEP reasons, grace, reference/lease claims and stale-plan invalidation
- [x] Add null/file adapter seams and manually triggered task-end reports; execution always blocked
- [ ] Build a low-overhead background macOS disk watcher and pressure alerts
- [x] Add live APS-managed producer lifecycle, references/leases and fenced generation/resume protection for newly created broker objects
- [x] Default managed collection to dry-run, with strict plans, identity checks and fixture-based negative tests
- [x] Collect explicitly approved flat managed files with durable intent/finish, crash recovery, tombstones, partial receipts and zero-count replay
- [x] Verify real installed producer/collector flows, thread/process fences and a device-bound synthetic command gateway fixture; no existing data adoption
- [ ] Automate task-end Scoop Reports and desktop notification adapters
- [x] Test cooperating concurrent producers and hardlink/symlink/identity/mount-unknown refusal in temporary fixtures
- [ ] Establish APFS clone/snapshot/open-file reclamation, hostile namespace/mount containment or hardware power-loss guarantees (currently unsupported)
- [ ] Complete exact final integrated authority/executor/adapter review, runtime checks and hosted supported-version/platform gates
- [ ] Publish an installable preview only after independent audit and publication gates

No private internal engineering repository, task transcript, account token or machine-specific configuration should be copied into this public project.
