# Phase 1: observe-only CLI

The package supports Python 3.10+ and uses only the standard library at runtime. It targets macOS/POSIX (`fcntl`, descriptor-relative directory operations, `O_NOFOLLOW`, and `statvfs`). There is no cleanup command, scheduler, watcher, lease service, notification service, or orchestrator adapter.

## Commands and storage

- `aps manifest init FILE --task-id ID --producer ID [--host HOST] [--root DIR ...]`
- `aps manifest record FILE PATH --class CLASS [--disposable]`
- `aps manifest validate FILE`
- `aps manifest show FILE [--public]`
- `aps event validate EVENTS.jsonl [--task-id ID]`
- `aps schema manifest` / `aps schema task-event`
- `aps telemetry FILE [--public]`
- `aps report FILE [--events EVENTS.jsonl] [--format json|markdown] [--public] [--output NEW_FILE]`

Exit status: 0 success, 1 validation/refusal or I/O error, 2 CLI usage error. Unknown fields, versions, classes, and events are rejected. Duplicate JSON keys, non-finite numbers, invalid timestamps, non-normalized paths, duplicate manifest paths, and disposable protected classes are rejected. The published JSON Schemas describe structure; the reference validator also enforces semantic constraints such as normalized paths, valid calendar dates, chronology, duplicate paths, and protected-class policy. Exported public manifests are display copies and are not valid input manifests.

Manifest init/record are idempotent; conflicting repeats fail without updating the manifest. Writes serialize on the parent directory's advisory lock and atomically replace the manifest with mode 0600. A failed write/rename may leave a mode-0600 staging file; Phase 1 does not remove it. Report output uses exclusive creation with mode 0600 and refuses existing files and symlinks. No artifact is modified by these operations. Keep manifests/report outputs outside measured scratch directories to avoid counting metadata as generated artifacts.

Metadata reads/writes also reject symlink ancestors. Manifest leaf reads, staging-file creation, and replacement use the same locked parent directory descriptor; a renamed parent cannot redirect them into a substituted directory. New report outputs use an anchored parent descriptor too. These operations select the original directory object: a concurrent rename can move that object, but cannot redirect writes through a replacement symlink. This is not a general namespace-containment or crash-durability guarantee.

Use physical absolute root paths. Symlinks in any traversal component are refused, including aliases such as `/tmp` or `/var` on macOS; the corresponding physical locations are commonly `/private/tmp` and `/private/var`. Relative CLI paths are made absolute, without silently resolving symlinks. Registered identities are checked at measurement time. This protects observation; it does not constitute a race-safe deletion executor.

## Events and accounting

Supply a JSON Lines file containing events like this (one object per line):

```json
{"schema":"agent-pooper-scooper/task-event","schema_version":1,"event_id":"evt-1","task_id":"demo-1","host":"example-host","producer":"example-agent","event_type":"task.completed","occurred_at":"2026-10-31T12:00:00Z"}
```

Events include started, paused, resumed, completed, failed, and cancelled. Report event logs must match the manifest task ID. Status uses the latest timestamp, with line order breaking ties; it does not authenticate event provenance or prove reference/lease safety. No supplied events means unknown task state and retain. A completed event triggers only reporting.

Event-file reads use an anchored no-follow/nonblocking descriptor and accept only regular UTF-8 files. Limits are 2 MiB per log, 10,000 lines including blank lines, and 16,384 bytes per line. FIFO/device/symlink inputs and oversized logs are refused before task status is used; no partial log is accepted. These data limits do not guarantee a wall-clock deadline for regular-file I/O on an unresponsive filesystem.

Walks count regular-file apparent sizes and allocated blocks, deduplicate hardlinks by device/inode, skip symlinks/special files/device boundaries, and record unreadable/changed/missing entries as unknown. Default bounds are 1,000,000 entries and depth 64 per walk; reaching a bound makes measurement incomplete. Detail lists cap at 200 entries per walk and 500 observations per report; reason counts remain exact. Bounds apply per walk, not to the entire manifest. Reports may measure artifacts and roots separately, so large manifests can require substantial work; no background polling is performed.

`--public` replaces registered-root prefixes and home/personal prefixes and redacts hostnames in manifests/reports. Relative filenames, task IDs, and producer identifiers remain visible; inspect exports before sharing. It does not read file contents, code, credentials, or transcripts.

Absolute paths outside known roots/home use a fully redacted fallback, including missing artifacts and embedded error details. Public CLI failures use generic diagnostics so invalid user-supplied names/values cannot escape on stderr; successful output-file messages also hide the destination. Non-public diagnostics retain detail for local troubleshooting.

## Local verification

```sh
python3 -B -m unittest discover -s tests -v
# With an approved locally provisioned setuptools 58+ and wheel:
python3 -m pip wheel --no-index --no-deps --no-build-isolation --wheel-dir /temporary/output .
python3 -m pip install --no-index --no-deps /temporary/output/agent_pooper_scooper-*.whl
python3 -B tests/installed_cli_smoke.py /path/to/installed/bin/aps
```

Build in a temporary source copy if the workspace must remain free of build output. `setuptools.build_meta.build_sdist` and `build_wheel` are also supported. The installed smoke flow runs subprocesses from a tempfile directory, without a source `PYTHONPATH`, and verifies telemetry, manifest/event/schema validation, JSON/Markdown reports, public redaction, unknown state, hardlink/symlink handling, zero removals, and output overwrite refusal.

The sdist includes the public safety tools and CI workflow needed by its source tests; the wheel contains the runtime package and license. CI preserves the required `Publication Gate / Python Tests` check and separately configures unit/safety, sdist-to-wheel, offline installation and installed-CLI coverage on standard Ubuntu/macOS runners with Python 3.10 and 3.15. Local results and hosted matrix results are separate evidence for each revision.

Packaging decision (local checkpoint, 2026-11-01): use the already installed setuptools 58.0.4 and wheel 0.37.0 through standard `setup.cfg`/PEP 517 configuration. The setuptools MIT license file and wheel MIT metadata were inspected. This avoids downloading a newer backend or maintaining a custom wheel builder. Build tools are not bundled into the product wheel; runtime requirements remain empty. No upstream source was copied. This is packaging/provenance evidence, not a toolchain security audit or a Phase 2 dependency decision.

## Remaining gates

Phase 2 requires independently reviewed cleanup policy, explicit owner-approved disposable roots, live/paused/resumable/dependent references, leases, retention grace periods, scoped authorization, executor identity rechecks, and auditable action outcomes. Also pending: task-end automation, notification verification/deduplication, disk pressure watching, and provider/desktop adapters.

Local tests do not establish APFS clone/snapshot reclaim behavior, real mounted-volume traversal, adversarial concurrent namespace mutation, power-loss durability, independent security acceptance, supported-version CI, or a published release. CI/publication tooling is a separate integration; this checkpoint does not claim those gates passed. No external implementation source was copied and no runtime dependency was added. Build tooling remains separate from the installed runtime.
