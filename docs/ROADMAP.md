# Roadmap

Phase 1 is an observe-only pre-alpha. Checked items describe local implementation and verification, not a published release or independent acceptance.

- [x] Define versioned provider-neutral task event and artifact manifest schemas with strict validation
- [x] Package a Python 3.10+ stdlib-runtime CLI with manifest init/record/validate/show
- [x] Implement on-demand read-only volume telemetry and bounded registered-root attribution
- [x] Generate JSON/Markdown Scoop Reports with public path redaction and explicit unknown accounting
- [x] Test hardlink deduplication, symlinks, permissions, identity mismatch, traversal limits and simulated mount boundaries in tempfile fixtures
- [x] Add a read-only regular-file planner with strict snapshot evidence, KEEP reasons, grace, reference/lease claims and stale-plan invalidation
- [x] Add null/file adapter seams and manually triggered task-end reports; execution always blocked
- [ ] Build a low-overhead background macOS disk watcher and pressure alerts
- [ ] Integrate task lifecycle, live-worker references, leases and session resume protection
- [ ] Implement deterministic dry-run safety policy with exhaustive negative tests
- [ ] Add narrowly scoped scratch cleanup, TOCTOU protections and audit receipts
- [ ] Automate task-end Scoop Reports and desktop notification adapters
- [ ] Test concurrent writes, Trash, APFS clones/snapshots, hard links and volume boundaries
- [ ] Verify independent security review, tests and macOS acceptance gates
- [ ] Publish an installable preview only after independent audit and publication gates

No private internal engineering repository, task transcript, account token or machine-specific configuration should be copied into this public project.
