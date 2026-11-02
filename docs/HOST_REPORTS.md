# Native disk reports

`aps host scan` measures existing native agent/project/cache locations immediately.
It does not need an orchestrator, broker, task manifest or running agent. Every
existing file stays **KEEP**. Location-based accounting does not establish a
producer, task owner, active/resumable status or deletion authority.

```sh
aps host scan --format markdown
aps host scan --root /physical/project --root /physical/agent-cache --public
aps host scan --root /physical/project --output /physical/new-report.json
aps host pressure --format markdown
aps host pressure --path /physical/volume --min-free-percent 15 --min-free-bytes 0
```

With no `--root`, scan selects only existing common locations relative to the
current home: AO worktrees, Claude projects, Codex sessions, Gemini temporary
data, shared application/package caches, and `Projects`. This is an inventory
of selected locations, not a full disk scan. A symlink location is reported as
skipped. Whole-home and filesystem-root scans are refused. Up to twelve explicit
roots are accepted; duplicate paths, hardlinks and overlapping walks are not
double-counted. With overlapping roots, a later row can show zero additional
bytes already counted in an earlier row.

The default budget is **20,000 entries per root** and depth **12**. Optional
`--max-entries` accepts 1–100,000 and `--max-depth` accepts 0–64. Limits,
unreadable paths, symlinks and mount boundaries are visible in the JSON report.
Partial totals are known lower bounds. The operation observes metadata and
never opens file contents. Filesystem changes during a scan can make the report
partial; this is not an atomic filesystem snapshot.

`aps host pressure` calls volume metadata APIs without traversing directories.
By default it flags low availability below **20 GiB** or **10%**. These thresholds
are configurable. A low-space result is an observation, not an instruction to
delete data. Unreadable capacity remains unknown.

Reports separate apparent file sizes, allocated blocks and volume availability.
They always report zero removed bytes and unknown reclaimed capacity. APFS
clones/snapshots, open files and other processes can make actual reclaimable
capacity differ from both file size and allocated blocks.

`--public` uses a whitelist projection: numerical bucket IDs replace root
paths, categories and labels; filenames, volume device IDs and detailed error
paths are omitted. Private reports contain selected paths and should be kept
locally. `--output` exclusively creates a new mode-0600 file and refuses existing
files or symlinks. It never updates an existing report. A report can be
successfully generated with `complete: false`; callers should inspect that
field and the qualified measurement totals rather than treating exit zero as
proof that every file was measured.

Neither command installs a background service, sends notifications, modifies
native data, nor enrolls existing roots in managed collection. A scheduler may
invoke the lightweight pressure check and choose fresh private report paths.
Managed cleanup remains a separate explicitly authorized closed-store workflow.
