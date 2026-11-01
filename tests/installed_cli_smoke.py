"""Run against an installed wheel: python3 tests/installed_cli_smoke.py /path/to/aps."""
import json
import hashlib
from datetime import datetime, timedelta, timezone
import os
import signal
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


    def test_installed_managed_producer_and_collection(self):
        # Short physical fixture path also fits Darwin Unix-socket limits.
        with tempfile.TemporaryDirectory(prefix="aps-wheel-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None) as tmp:
            base = Path(tmp).resolve()
            store, key, creds, socket_path = (base / name for name in ("store", "owner.json", "producer.json", "broker.sock"))
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            def run(*args, expected=0):
                result = subprocess.run([APS, *map(str, args)], cwd=base, env=environment,
                                        capture_output=True, text=True, timeout=12)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if "--public" in args:
                    self.assertNotIn(str(base), result.stdout + result.stderr)
                return json.loads(result.stdout) if result.stdout else None
            run("managed", "init", store, "--owner-key-output", key)
            owner_secret = json.loads(key.read_text())["owner_key"]
            broker = subprocess.Popen([APS, "managed", "serve", str(store), "--owner-key-file", str(key),
                                       "--socket", str(socket_path)], cwd=base, env=environment,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not socket_path.exists() and broker.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(broker.poll(), "installed broker exited before readiness")
                self.assertTrue(socket_path.exists())
                prefix = ("managed", "producer", "--socket", socket_path)
                run(*prefix, "register", "--owner-key-file", key, "--task-id", "wheel-task", "--producer-id", "fixture",
                    "--credentials-output", creds, "--request-id", "register-wheel")
                token = json.loads(creds.read_text())["credentials"]["token"]
                prefix += ("--credentials-file", creds)
                run(*prefix, "start", "--request-id", "start-wheel")
                payload, protected = base / "synthetic.bin", base / "retained.bin"
                payload.write_bytes(b"wheel-only generated fixture")
                protected.write_bytes(b"protected original fixture")
                ident = run(*prefix, "write", "--input", payload, "--disposable", "--request-id", "write-wheel")["artifact_id"]
                keep = run(*prefix, "write", "--input", protected, "--class", "session-history", "--request-id", "write-protected")["artifact_id"]
                claim = run(*prefix, "lease", "--artifact-id", ident, "--expires-at", "2000-01-01T00:00:00Z", "--request-id", "claim-wheel")["claim_id"]
                owner = ("managed",)
                owner_args = lambda command: ("managed", command, store, "--owner-key-file", key)
                active = run(*owner_args("plan"), "--task-id", "wheel-task", "--public")
                self.assertEqual(active["candidate"]["count"], 0)
                run(*prefix, "terminal", "--request-id", "terminal-wheel")
                time.sleep(1.05)
                held = run(*owner_args("plan"), "--task-id", "wheel-task", "--public")
                self.assertEqual(held["candidate"]["count"], 0)
                self.assertTrue(any("lease_present" in decision["reasons"] for decision in held["decisions"]))
                # Terminal does not clear an expired lease. A fresh owner-resumed
                # producer explicitly releases its holder's previous generation.
                resumed = base / "resumed.json"
                run(*prefix, "resume", "--owner-key-file", key, "--credentials-output", resumed, "--request-id", "resume-wheel")
                old_read = run(*prefix, "read", "--artifact-id", keep, "--output", base / "old-read", expected=1)
                prefix = ("managed", "producer", "--socket", socket_path, "--credentials-file", resumed)
                run(*prefix, "release", "--claim-id", claim, "--request-id", "release-wheel")
                run(*prefix, "terminal", "--request-id", "terminal-wheel-2")
                time.sleep(1.05)
                plan_path, grant_path = base / "plan.json", base / "grant.json"
                run(*owner_args("plan"), "--task-id", "wheel-task", "--output", plan_path)
                plan = json.loads(plan_path.read_text())
                self.assertEqual(plan["candidate"]["artifact_ids"], [ident])
                self.assertEqual(run(*owner_args("collect"), "--plan", plan_path)["status"], "DRY_RUN")
                run(*owner_args("approve"), "--plan", plan_path, "--operation-id", "wheel-collect", "--output", grant_path)
                grant = json.loads(grant_path.read_text())
                output = base / "existing-report"
                output.write_bytes(b"preserve report")
                run(*owner_args("collect"), "--plan", plan_path, "--operation-id", "wheel-collect", "--approval-id", grant["approval_id"],
                    "--execute", "--output", output, "--public", expected=1)
                self.assertEqual(output.read_bytes(), b"preserve report")
                result = run(*owner_args("collect"), "--plan", plan_path, "--operation-id", "wheel-collect", "--approval-id", grant["approval_id"], "--execute", "--public")
                self.assertEqual(result["removed"], {"count": 1, "bytes": len(payload.read_bytes())})
                self.assertIsNone(result["reclaimed"]["bytes"])
                unified_path = base / "unified-report.json"
                run(*owner_args("report"), "--task-id", "wheel-task", "--output", unified_path, "--public")
                unified = json.loads(unified_path.read_text())
                self.assertEqual(unified["generated"]["apparent_bytes"], len(payload.read_bytes()) + len(protected.read_bytes()))
                self.assertEqual(unified["retained"]["apparent_bytes"], len(protected.read_bytes()))
                self.assertEqual(unified["removed"]["bytes"], len(payload.read_bytes()))
                self.assertEqual(unified["removed"]["scope"], "cumulative_confirmed_receipts")
                self.assertEqual(unified["unknowns"]["count"], 0)
                self.assertIsNone(unified["reclaimed"]["bytes"])
                # Hermetic delivery of the WHOLE generated report, never a real notification.
                report_bridge = base / "report-bridge.py"
                report_bridge.write_text("import json,sys\nrequest=json.load(sys.stdin)\n"
                                         "assert request['tool']=='notify'\n"
                                         "assert 'Reclaimed capacity unknown.' in request['arguments']['message']\n"
                                         "print(json.dumps({'content':[{'type':'text','text':'Notification sent.'}]}))\n")
                delivered = run("integrations", "notice", store, "--owner-key-file", key,
                                "--report", unified_path, "--operation-id", "wheel-unified-notice",
                                "--device-id", "fixture-device", "--mission-id", "fixture-mission",
                                "--bridge-command-json", json.dumps([sys.executable, "-I", "-B", str(report_bridge)]),
                                "--send", "--test-mode", "--public")
                self.assertEqual(delivered["status"], "provider_acknowledged")
                self.assertFalse(delivered["user_seen_verified"])
                replay = run(*owner_args("collect"), "--plan", plan_path, "--operation-id", "wheel-collect", "--approval-id", grant["approval_id"], "--execute")
                self.assertEqual(replay["status"], "IDEMPOTENT_REPLAY")
                self.assertEqual(replay["removed"]["bytes"], 0)
                for private in (owner_secret, token, str(base)):
                    self.assertNotIn(private, json.dumps(result))
                self.assertEqual(payload.read_bytes(), b"wheel-only generated fixture")
                self.assertEqual(protected.read_bytes(), b"protected original fixture")
                self.assertTrue((store / "control/lock").exists())
                self.assertTrue((store / "control/registry.sqlite3").exists())
                # Fresh installed API reads the durable registry through its
                # owner capability, never by editing raw database state.
                probe = ("import json,sys;from agent_pooper_scooper.authority import ManagedAuthority;"
                         "key=json.load(open(sys.argv[2]))['owner_key'];a=ManagedAuthority(sys.argv[1],owner_key=key);"
                         "s=a.snapshot('wheel-task');items={x['artifact_id']:x for x in s['inventory']};"
                         "assert a.receipt('wheel-collect',sys.argv[3])['outcome']=='unlinked';"
                         "from pathlib import Path;root=Path(s['root']['path']);"
                         "assert not (root/items[sys.argv[3]]['name']).exists();"
                         "assert (root/items[sys.argv[4]]['name']).read_bytes()==b'protected original fixture';"
                         "print('durable installed receipt and protected bytes verified')")
                check = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(store), str(key), ident, keep],
                                       cwd=base, env=environment, text=True, capture_output=True, timeout=8)
                self.assertEqual(check.returncode, 0, check.stderr)
            finally:
                if broker.poll() is None:
                    broker.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = broker.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.communicate()
                    self.fail("own installed broker did not stop within lifecycle deadline")
                self.assertNotIn(owner_secret, stdout + stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(broker.returncode, 0, stderr)

    def test_installed_integration_routing_with_hermetic_bridge(self):
        with tempfile.TemporaryDirectory(prefix="aps-route-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None) as tmp:
            base = Path(tmp).resolve()
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            def run(*args, expected=0):
                result = subprocess.run([APS, *map(str, args)], cwd=base, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn(str(base), result.stdout + result.stderr)
                return json.loads(result.stdout) if result.stdout else None
            executable = base / "ao-fixture"
            executable.write_text("#!" + sys.executable + "\nimport json,sys\n"
                                  "assert sys.argv[1:]==['session','get','fixture-session','--json']\n"
                                  "print(json.dumps({'session':{'id':'fixture-session','projectId':'fixture-project','status':'working','private':'discard me'}}))\n")
            executable.chmod(0o700)
            observed = run("integrations", "observe-ao", "--session-id", "fixture-session", "--project-id", "fixture-project",
                           "--executable", executable, "--public")
            self.assertTrue(observed["identity_verified"])
            self.assertEqual(observed["status"], "active")
            self.assertFalse(observed["can_authorize_cleanup"])
            run("managed", "init", base / "store", "--owner-key-output", base / "owner.json")
            report = base / "report.json"
            report.write_text(json.dumps({"removed": {"count": 0, "bytes": 0}, "private": str(base)}))
            bridge = base / "bridge.py"
            bridge.write_text("import json,sys\n"
                              "request=json.load(sys.stdin)\n"
                              "assert request['tool']=='notify' and request['arguments']['deviceId']=='fixture-device'\n"
                              "assert request['arguments']['title']=='APS product integration TEST'\n"
                              "assert 'Reclaimed capacity unknown.' in request['arguments']['message']\n"
                              "print(json.dumps({'content':[{'type':'text','text':'Notification sent.'}]}))\n")
            prefix = ("integrations", "notice", base / "store", "--owner-key-file", base / "owner.json", "--report", report,
                      "--operation-id", "fixture-notice", "--device-id", "fixture-device", "--mission-id", "fixture-mission")
            self.assertEqual(run(*prefix, "--public")["status"], "not_attempted")
            self.assertIsNone(run(*prefix, "--send", "--stdio", "--public", expected=1))
            receipt = run(*prefix, "--send", "--test-mode", "--bridge-command-json", json.dumps([sys.executable, "-I", "-B", str(bridge)]), "--public")
            self.assertEqual(receipt["status"], "provider_acknowledged")
            self.assertFalse(receipt["user_seen_verified"])
            status = run("integrations", "notice-status", base / "store", "--owner-key-file", base / "owner.json",
                         "--operation-id", "fixture-notice", "--device-id", "fixture-device", "--mission-id", "fixture-mission", "--public")
            self.assertEqual(status, receipt)

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
