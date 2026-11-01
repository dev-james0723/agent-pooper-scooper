"""Run against an installed wheel: python3 tests/installed_cli_smoke.py /path/to/aps."""
import json
import hashlib
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

APS = None


class InstalledCliSmoke(unittest.TestCase):
    def test_installed_read_only_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "scratch"
            root.mkdir()
            target = root / "private-candidate.bin"
            target.write_bytes(b"p" * 17)
            path, evidence_path = base / "manifest.json", base / "evidence.json"
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)

            def run(*args, expected=0):
                process = subprocess.run([APS, *map(str, args)], cwd=base, env=environment,
                                         text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, expected, process.stderr)
                if any(str(a).startswith("--pub") for a in args):
                    for private in (str(base), "private-candidate.bin", "private-task", "private-host", "duplicate-key-private"):
                        self.assertNotIn(private, process.stdout + process.stderr)
                return process.stdout

            run("manifest", "init", path, "--task-id", "private-task", "--host", "private-host",
                "--producer", "fixture", "--root", root)
            run("manifest", "record", path, target, "--class", "temp", "--disposable")
            history = root / "Private-History"
            history.mkdir()
            alternate = root / "private-history"
            protected_child = (alternate if alternate.exists() else history) / "output.log"
            protected_child.write_bytes(b"retained history")
            run("manifest", "record", path, protected_child, "--class", "log", "--disposable")
            run("manifest", "record", path, history, "--class", "session-history")
            self.assertEqual(json.loads(run("plan", path))["candidate"]["count"], 0)
            self.assertEqual(json.loads(run("plan", path, "--execute", expected=1))["execution"]["status"], "BLOCKED")
            document = json.loads(path.read_text(encoding="utf-8"))
            # The operator explicitly selects a one-second fixture-only grace.
            time.sleep(1.1)
            now = datetime.now(timezone.utc)
            stamp = lambda seconds: (now + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            st = root.stat()
            evidence = {"schema": "agent-pooper-scooper/planner-evidence", "schema_version": 1,
                        "revision": "fixture-1", "task_id": "private-task", "host": "private-host",
                        "producer": "fixture-owner", "observed_at": stamp(0), "expires_at": stamp(240),
                        "manifest_digest": hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                        "task_status": "completed", "terminal_at": stamp(-7200),
                        "references_complete": True, "references": [], "leases_complete": True, "leases": [],
                        "mounts_complete": True, "mount_points": [], "worktrees_complete": True, "protected_paths": [],
                        "approved_roots": [{"path": str(root), "dev": st.st_dev, "ino": st.st_ino,
                                            "uid": st.st_uid, "approved": True, "approved_by": "fixture-owner",
                                            "approved_at": stamp(-10800), "grace_seconds": 1}]}
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            plan = json.loads(run("plan", path, "--evidence", evidence_path, "--public"))
            self.assertEqual(plan["candidate"]["apparent_bytes"], 17)
            inherited = [item for item in plan["decisions"] if "inside_retained_artifact" in item["reasons"]]
            self.assertEqual(len(inherited), 1)
            self.assertEqual(inherited[0]["decision"], "KEEP")
            self.assertEqual(plan["execution"]["status"], "BLOCKED")
            self.assertFalse(plan["evidence"]["authenticated"])
            self.assertIsNone(plan["reclaimed"]["bytes"])
            run("plan", path, "--evidence", evidence_path, "--public", "--execute", expected=1)
            event_path = base / "events.jsonl"
            event = {"schema": "agent-pooper-scooper/task-event", "schema_version": 1,
                     "event_id": "completed-1", "task_id": "private-task", "host": "private-host",
                     "producer": "fixture", "event_type": "task.completed", "occurred_at": stamp(-7200)}
            event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            data = json.loads(run("task-end", path, "--events", event_path, "--evidence", evidence_path, "--public"))
            self.assertEqual(data["eligible"]["apparent_bytes"], 17)
            self.assertEqual(data["removed"]["count"], 0)
            self.assertEqual(data["notification"]["status"], "not_attempted")
            self.assertIsNone(data["capacity"]["reclaimed_estimate"]["bytes"])
            self.assertIn("Execution: **BLOCKED**", run("task-end", path, "--events", event_path,
                          "--evidence", evidence_path, "--public", "--format", "markdown"))
            evidence["references_complete"] = False
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            self.assertEqual(json.loads(run("plan", path, "--evidence", evidence_path))["candidate"]["count"], 0)
            bad = base / "bad-private-manifest.json"
            key = "duplicate-key-private"
            bad.write_text('{"' + key + '":1,"' + key + '":2}', encoding="utf-8")
            for command in (("report",), ("telemetry",), ("manifest", "show")):
                self.assertNotIn(key, run(*command, bad, "--public", expected=1))
            run("report", base / "missing-private.json", "--pub", expected=2)
            fifo = base / "events-fifo"
            os.mkfifo(fifo)
            run("task-end", path, "--events", fifo, "--public", expected=1)
            oversized = base / "oversized-events.jsonl"
            oversized.write_bytes(b" " * (2 * 1024 * 1024 + 1))
            run("task-end", path, "--events", oversized, "--public", expected=1)
            linked = base / "linked-events.jsonl"
            linked.symlink_to(event_path)
            run("task-end", path, "--events", linked, "--public", expected=1)
            self.assertEqual(target.read_bytes(), b"p" * 17)
            self.assertEqual(protected_child.read_bytes(), b"retained history")
            # Mock HOME in the installed process without changing environment/config.
            selected = base / "SCRATCH"
            if not selected.exists():
                selected = root
            scoped = json.loads(path.read_text(encoding="utf-8"))
            scoped["roots"][0]["path"] = str(selected)
            for artifact in scoped["artifacts"]:
                artifact["path"] = str(selected / Path(artifact["path"]).relative_to(root))
            scoped_path = base / "scoped-manifest.json"
            scoped_path.write_text(json.dumps(scoped), encoding="utf-8")
            snapshot = dict(evidence, references_complete=True)
            snapshot["approved_roots"] = [dict(evidence["approved_roots"][0], path=str(selected))]
            snapshot["manifest_digest"] = hashlib.sha256(json.dumps(
                scoped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            scoped_evidence = base / "scoped-evidence.json"
            scoped_evidence.write_text(json.dumps(snapshot), encoding="utf-8")
            script = ("import sys; from unittest import mock; "
                      "from agent_pooper_scooper import cli,disk\n"
                      "with mock.patch('os.path.expanduser', return_value=sys.argv[3]), "
                      "mock.patch.object(disk,'walk_usage',side_effect=AssertionError('fixture home scan forbidden')):\n"
                      " raise SystemExit(cli.main(['plan',sys.argv[1],'--evidence',sys.argv[2],'--public']))\n")
            process = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                      str(scoped_path), str(scoped_evidence), str(root)],
                                     cwd=base, env=environment, text=True, capture_output=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertNotIn(str(base), process.stdout + process.stderr)
            refused = json.loads(process.stdout)
            self.assertEqual(refused["candidate"]["count"], 0)
            self.assertTrue(all("root_scope_too_broad" in item["reasons"] for item in refused["decisions"]))
            self.assertEqual(refused["execution"]["status"], "BLOCKED")
            self.assertEqual(protected_child.read_bytes(), b"retained history")

    def test_installed_observe_only_workflow(self):
        self.assertIsNotNone(APS, "pass the installed aps executable")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "scratch"
            build = root / "build"
            build.mkdir(parents=True)
            (build / "out.bin").write_bytes(b"b" * 64)
            os.link(build / "out.bin", build / "hardlink.bin")
            source = root / "source.txt"
            source.write_bytes(b"s" * 32)
            outside = base / "outside.bin"
            outside.write_bytes(b"p" * 8192)
            os.symlink(outside, root / "symlink.bin")
            missing = root / "gone.bin"
            missing.write_bytes(b"m" * 16)
            manifest = base / "manifest.json"
            event_log = base / "events.jsonl"
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)

            def run(*args, expected=0):
                process = subprocess.run([APS, *map(str, args)], cwd=base, env=environment,
                                         text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, expected, process.stderr)
                if "--public" in args:
                    self.assertNotIn(str(base), process.stdout + process.stderr)
                return process.stdout

            self.assertIn("aps 0.1.0.dev0", run("--version"))
            run("manifest", "init", manifest, "--task-id", "smoke-1", "--producer", "fixture",
                "--host", "fixture-host", "--root", root)
            run("manifest", "record", manifest, build, "--class", "build-output", "--disposable")
            run("manifest", "record", manifest, source, "--class", "source")
            run("manifest", "record", manifest, missing, "--class", "temp", "--disposable")
            missing.rename(base / "retained-gone.bin")
            run("manifest", "validate", manifest)
            self.assertEqual(len(json.loads(run("manifest", "show", manifest))["artifacts"]), 3)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

            event = {"schema": "agent-pooper-scooper/task-event", "schema_version": 1,
                     "event_id": "smoke-completed", "task_id": "smoke-1", "host": "fixture-host",
                     "producer": "fixture", "event_type": "task.completed",
                     "occurred_at": "2026-10-31T13:00:00Z"}
            event_log.write_text(json.dumps(event) + "\n", encoding="utf-8")
            run("event", "validate", event_log, "--task-id", "smoke-1")
            for kind in ("manifest", "task-event"):
                self.assertFalse(json.loads(run("schema", kind))["additionalProperties"])

            def snapshot():
                return {str(p): (p.lstat().st_ino, p.lstat().st_size, p.lstat().st_mtime_ns)
                        for p in root.rglob("*")}

            before = snapshot()
            telemetry = json.loads(run("telemetry", manifest, "--public"))
            self.assertEqual(telemetry["roots"][0]["usage"]["apparent_bytes"], 96)
            self.assertEqual(telemetry["roots"][0]["usage"]["duplicates"], 1)
            self.assertEqual(telemetry["roots"][0]["usage"]["skipped_by_reason"]["symlink"], 1)
            self.assertGreater(telemetry["volumes"][0]["total_bytes"], 0)
            data = json.loads(run("report", manifest, "--events", event_log, "--public"))
            self.assertEqual(data["generated"]["apparent_bytes"], 96)
            self.assertEqual(data["retained"]["apparent_bytes"], 32)
            self.assertEqual(data["eligible"]["apparent_bytes"], 64)
            self.assertEqual(data["unknowns"]["by_reason"]["missing"], 1)
            self.assertEqual(data["generated"]["confidence"], "partial")
            self.assertEqual(data["removed"]["count"], 0)
            self.assertEqual(data["removed"]["receipts"], [])
            self.assertEqual(data["capacity"]["reclaimed_estimate"]["bytes"], 0)
            self.assertEqual(data["notification"]["status"], "not_attempted")
            self.assertIsNotNone(data["capacity"]["volumes"][0]["available_after_bytes"])
            self.assertEqual(data["audit"][-1]["action"], "report.completed")
            self.assertEqual(data["task"]["host"], "<redacted-host>")
            markdown = run("report", manifest, "--events", event_log, "--format", "markdown", "--public")
            self.assertIn("## Unknowns", markdown)
            self.assertNotIn(str(base), markdown + json.dumps(data) + json.dumps(telemetry))
            unknown_status = json.loads(run("report", manifest))
            self.assertEqual(unknown_status["task"]["status"], "unknown")
            self.assertEqual(unknown_status["eligible"]["count"], 0)

            output = base / "report.json"
            run("report", manifest, "--public", "--output", output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            output_before = output.read_bytes()
            run("report", manifest, "--public", "--output", output, expected=1)
            self.assertEqual(output.read_bytes(), output_before)
            invalid = base / "invalid.json"
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["unknown_field"] = True
            invalid.write_text(json.dumps(document), encoding="utf-8")
            run("manifest", "validate", invalid, expected=1)
            event["event_type"] = "task.purge"
            event_log.write_text(json.dumps(event) + "\n", encoding="utf-8")
            run("event", "validate", event_log, expected=1)
            run("report", manifest, "--events", event_log, expected=1)
            manifest_before = manifest.read_bytes()
            run("manifest", "record", manifest, source, "--class", "source", "--disposable", expected=1)
            self.assertEqual(manifest.read_bytes(), manifest_before)
            run("cleanup", manifest, expected=2)
            run("report", manifest, "--delete", expected=2)
            self.assertEqual(before, snapshot())
            self.assertEqual(outside.read_bytes(), b"p" * 8192)


if __name__ == "__main__":
    APS = str(Path(sys.argv.pop(1)).resolve())
    unittest.main(verbosity=2)
