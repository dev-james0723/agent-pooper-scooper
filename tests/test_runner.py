"""Task runner tests use new private fixtures and literal local commands only."""
import io
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import executor_platform, runner
from agent_pooper_scooper.authority import ProducerClient


class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.run_dir = self.base / "run"
        self.out, self.err = io.BytesIO(), io.BytesIO()

    def run_task(self, source, *args, **kwargs):
        return runner.run_command([sys.executable, "-c", source, *map(str, args)], str(self.run_dir),
                                  stdout=self.out, stderr=self.err, **kwargs)

    def blobs(self):
        return list((self.run_dir / "store" / "artifacts").glob("*.blob"))

    def fixture_mount(self):
        # The container filesystem may be overlayfs, intentionally unsupported
        # by the production collector. Only native mount observation is mocked;
        # broker ownership, exact plan/grant, identity checks and unlink are real.
        return mock.patch.object(executor_platform, "observe",
                                 side_effect=lambda fd, path: {"canonical_root": path, "backend": "fixture"})

    def test_default_preserves_new_output_and_private_state(self):
        receipt, code = self.run_task("import sys; print('hello'); sys.stderr.write('warning\\n')")
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), b"hello\n")
        self.assertEqual(self.err.getvalue(), b"warning\n")
        self.assertTrue(receipt["capture"]["complete"])
        self.assertEqual(receipt["capture"]["stored_bytes"], 14)
        self.assertEqual(receipt["cleanup"]["status"], "retained")
        self.assertEqual(receipt["cleanup"]["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(sorted(p.read_bytes() for p in self.blobs()), [b"hello\n", b"warning\n"])
        self.assertEqual(json.loads((self.run_dir / "report.json").read_text()), receipt)
        self.assertEqual(stat.S_IMODE(self.run_dir.stat().st_mode), 0o700)
        for name in ("report.json", "owner.json"):
            self.assertEqual(stat.S_IMODE((self.run_dir / name).stat().st_mode), 0o600)

    def test_clean_success_removes_only_broker_output_and_keeps_command_files(self):
        sentinel = self.base / "unmanaged-canary"
        sentinel.write_bytes(b"original")
        generated = self.base / "command-created"
        source = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); p.mkdir(); "
                  "(p/'source.py').write_text('precious source'); "
                  "(p/'session.json').write_text('resume material'); print('disposable')")
        with self.fixture_mount():
            receipt, code = self.run_task(source, generated, clean_success=True)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["cleanup"]["status"], "COMPLETED")
        self.assertEqual(receipt["cleanup"]["removed"], {"count": 1, "bytes": 11})
        self.assertEqual(receipt["reclaimed"]["bytes"], None)
        self.assertEqual(self.blobs(), [])
        self.assertEqual(sentinel.read_bytes(), b"original")
        self.assertEqual((generated / "source.py").read_text(), "precious source")
        self.assertEqual((generated / "session.json").read_text(), "resume material")
        self.assertTrue((self.run_dir / "store" / "control" / "registry.sqlite3").exists())

    def test_command_failure_retains_disposable_output_without_approval(self):
        with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
            receipt, code = self.run_task("import sys; print('diagnostic'); sys.exit(7)", clean_success=True)
        self.assertEqual(code, 7)
        self.assertEqual(receipt["command"]["exit_code"], 7)
        self.assertEqual(receipt["command"]["capture_status"], "complete")
        self.assertEqual(receipt["cleanup"]["status"], "retained")
        self.assertEqual(self.blobs()[0].read_bytes(), b"diagnostic\n")

    def test_output_ceiling_retains_bounded_data_and_never_approves(self):
        with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
            receipt, code = self.run_task("import os; os.write(1, b'x'*10000)",
                                          clean_success=True, max_output_bytes=1024)
        self.assertEqual(code, 125)
        self.assertEqual(receipt["command"]["capture_status"], "output_limit")
        self.assertFalse(receipt["capture"]["complete"])
        self.assertEqual(receipt["capture"]["stored_bytes"], 1024)
        self.assertEqual(self.out.getvalue(), b"x" * 1024)
        self.assertEqual(sum(p.stat().st_size for p in self.blobs()), 1024)

    def test_two_stream_chunks_are_exact_and_larger_than_one_object(self):
        size = runner.MAX_BYTES + 1
        receipt, code = self.run_task("import os,sys; n=int(sys.argv[1]); os.write(1,b'a'*n); os.write(2,b'b'*n)", size)
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), b"a" * size)
        self.assertEqual(self.err.getvalue(), b"b" * size)
        self.assertEqual(receipt["capture"]["stored_bytes"], size * 2)
        self.assertEqual(len(receipt["capture"]["objects"]), 4)
        self.assertEqual(receipt["cleanup"]["removed"], {"count": 0, "bytes": 0})
        self.assertTrue(all(p.stat().st_size <= runner.MAX_BYTES for p in self.blobs()))

    def test_output_ceiling_is_shared_by_both_streams(self):
        receipt, code = self.run_task("import os; os.write(1,b'a'*1000); os.write(2,b'b'*1000)",
                                      max_output_bytes=1500, clean_success=True)
        self.assertEqual(code, 125)
        self.assertEqual(receipt["capture"]["stored_bytes"], 1500)
        self.assertEqual(sum(receipt["capture"]["streams"].values()), 1500)
        self.assertEqual(sum(p.stat().st_size for p in self.blobs()), 1500)
        self.assertFalse(receipt["cleanup"]["attempted"])

    def test_exact_ceiling_is_complete_when_no_byte_exceeds_it(self):
        receipt, code = self.run_task("import os; os.write(1,b'x'*1024)", max_output_bytes=1024)
        self.assertEqual(code, 0)
        self.assertTrue(receipt["capture"]["complete"])

    def test_timeout_stops_own_child_and_retains_already_read_output(self):
        before = time.monotonic()
        with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
            receipt, code = self.run_task("import sys,time; print('before wait',flush=True); time.sleep(30)",
                                          timeout=0.3, clean_success=True)
        self.assertLess(time.monotonic() - before, 5)
        self.assertEqual(code, 124)
        self.assertEqual(receipt["command"]["capture_status"], "timed_out")
        self.assertEqual(self.blobs()[0].read_bytes(), b"before wait\n")

    def test_descendant_holding_pipes_is_not_complete_capture(self):
        source = "import os,time; pid=os.fork(); time.sleep(30) if pid==0 else None"
        with mock.patch.object(runner, "PIPE_DRAIN_SECONDS", 0.2):
            receipt, code = self.run_task(source, timeout=5, clean_success=True)
        self.assertEqual(code, 125)
        self.assertEqual(receipt["command"]["capture_status"], "incomplete_pipes")
        self.assertFalse(receipt["cleanup"]["attempted"])

    def test_existing_or_symlink_run_directory_is_never_adopted(self):
        self.run_dir.mkdir()
        keep = self.run_dir / "keep"
        keep.write_bytes(b"retained")
        with self.assertRaises(FileExistsError):
            self.run_task("raise SystemExit('must not execute')")
        self.assertEqual(keep.read_bytes(), b"retained")
        link = self.base / "link"
        link.symlink_to(self.run_dir, target_is_directory=True)
        with self.assertRaises((OSError, runner.RunnerError)):
            runner.run_command([sys.executable, "-c", "pass"], str(link))
        with self.assertRaises((OSError, runner.RunnerError)):
            runner.run_command([sys.executable, "-c", "pass"], str(link / "nested"))
        self.assertFalse((self.run_dir / "nested").exists())

    def test_world_writable_parent_and_invalid_arguments_refuse_before_execution(self):
        public = self.base / "public"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        with self.assertRaises(runner.RunnerError):
            runner.run_command([sys.executable, "-c", "pass"], str(public / "new"))
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"max_output_bytes": 0},
                       {"max_output_bytes": runner.MAX_OUTPUT_BYTES + 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(runner.RunnerError):
                self.run_task("pass", **kwargs)
        self.assertFalse(self.run_dir.exists())

    def test_arguments_environment_and_output_never_enter_receipt(self):
        private_value = "private-fixture-argument"
        with mock.patch.dict(os.environ, {"APS_FIXTURE_VALUE": "private-fixture-environment"}):
            receipt, code = self.run_task("import os,sys; print(sys.argv[1]); print(os.environ['APS_FIXTURE_VALUE'])", private_value)
        self.assertEqual(code, 0)
        encoded = json.dumps(receipt)
        self.assertNotIn(private_value, encoded)
        self.assertNotIn("private-fixture-environment", encoded)
        self.assertNotIn("sys.argv", encoded)
        self.assertNotIn(str(self.base), encoded)
        self.assertNotIn(json.loads((self.run_dir / "owner.json").read_text())["owner_key"], encoded)

    def test_literal_argument_is_never_interpreted_by_shell(self):
        literal = "$(printf not-a-shell); ignored &"
        receipt, code = self.run_task("import sys; print(sys.argv[1])", literal)
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), (literal + "\n").encode())

    def test_broker_failure_stops_capture_and_never_replays_or_approves(self):
        with mock.patch.object(ProducerClient, "write", side_effect=RuntimeError("private-error")) as write:
            with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
                receipt, code = self.run_task("print('hello')", clean_success=True)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(code, 125)
        self.assertEqual(receipt["command"]["capture_status"], "capture_error")
        self.assertEqual(receipt["cleanup"]["status"], "not_completed_inspect_receipts")
        self.assertNotIn("private-error", json.dumps(receipt))

    def test_unsupported_collection_retains_output_and_reports_refusal(self):
        with mock.patch.object(runner.executor, "create_plan", side_effect=ValueError("unsupported fixture")):
            receipt, code = self.run_task("print('keep me')", clean_success=True)
        self.assertEqual(code, 0)  # The command succeeded; cleanup has its own result.
        self.assertEqual(receipt["cleanup"]["status"], "not_completed_inspect_receipts")
        self.assertEqual(self.blobs()[0].read_bytes(), b"keep me\n")

    def test_user_interruption_retains_bytes_and_stops_only_owned_command(self):
        with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
            with mock.patch.object(runner, "_mirror", side_effect=KeyboardInterrupt()):
                receipt, code = self.run_task("import time; print('before interrupt',flush=True); time.sleep(30)",
                                              clean_success=True)
        self.assertEqual(code, 130)
        self.assertEqual(receipt["command"]["capture_status"], "interrupted")
        self.assertEqual(self.blobs()[0].read_bytes(), b"before interrupt\n")

    def test_post_write_broker_ambiguity_does_not_retry_or_collect(self):
        real = ProducerClient.write

        def write_then_lose_ack(client, *args, **kwargs):
            real(client, *args, **kwargs)
            raise RuntimeError("fixture lost acknowledgement")

        with mock.patch.object(ProducerClient, "write", write_then_lose_ack):
            with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("must retain")):
                receipt, code = self.run_task("print('persisted before lost acknowledgement')", clean_success=True)
        self.assertEqual(code, 125)
        self.assertEqual(len(self.blobs()), 1)
        self.assertFalse(receipt["cleanup"]["attempted"])
        self.assertEqual(receipt["capture"]["stored_bytes"], 0)  # No acknowledgement was received.
        self.assertGreater(receipt["accounting"]["generated"]["apparent_bytes"], 0)

    def test_mirror_restores_original_fd_flags_after_success_and_backpressure(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        with os.fdopen(write_fd, "wb", buffering=0) as sink:
            # A plain first write changes kernel-maintained F_GETFL bits on
            # Darwin. Establish that control before comparing restored flags;
            # _mirror must preserve the caller's actual I/O mode.
            os.write(write_fd, b"control")
            self.assertEqual(os.read(read_fd, 7), b"control")
            original = fcntl.fcntl(write_fd, fcntl.F_GETFL)
            runner._mirror(sink, b"prefix", deadline=time.monotonic() + 0.2)
            self.assertEqual(fcntl.fcntl(write_fd, fcntl.F_GETFL), original)
            self.assertEqual(os.read(read_fd, 6), b"prefix")
            with self.assertRaises(runner._MirrorTimeout):
                runner._mirror(sink, b"x" * (512 * 1024), deadline=time.monotonic() + 0.05)
            self.assertEqual(fcntl.fcntl(write_fd, fcntl.F_GETFL), original)

    def test_mirror_preserves_an_initially_nonblocking_descriptor(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        with os.fdopen(write_fd, "wb", buffering=0) as sink:
            os.write(write_fd, b"control")
            self.assertEqual(os.read(read_fd, 7), b"control")
            os.set_blocking(write_fd, False)
            original = fcntl.fcntl(write_fd, fcntl.F_GETFL)
            runner._mirror(sink, b"bytes", deadline=time.monotonic() + 0.2)
            self.assertEqual(fcntl.fcntl(write_fd, fcntl.F_GETFL), original)

    def test_arbitrary_python_sink_is_refused_without_calling_write_or_flush(self):
        class UnknownSink:
            def write(self, data):
                raise AssertionError("arbitrary write must not be called")

            def flush(self):
                raise AssertionError("arbitrary flush must not be called")

        with self.assertRaises(runner.RunnerError):
            runner._mirror(UnknownSink(), b"value", deadline=time.monotonic() + 0.2)

    def test_interrupt_during_broker_write_is_not_retried(self):
        real = ProducerClient.write
        calls = []

        def write_then_interrupt(client, *args, **kwargs):
            calls.append(kwargs["request_id"])
            real(client, *args, **kwargs)
            raise KeyboardInterrupt()

        with mock.patch.object(ProducerClient, "write", write_then_interrupt):
            receipt, code = self.run_task("print('written once')", clean_success=True)
        self.assertEqual(code, 130)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.blobs()), 1)
        self.assertFalse(receipt["cleanup"]["attempted"])

    def test_empty_output_needs_no_owner_approval(self):
        with mock.patch.object(runner.executor, "approve_plan", side_effect=AssertionError("no objects")):
            receipt, code = self.run_task("pass", clean_success=True)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["cleanup"]["status"], "nothing_to_collect")


if __name__ == "__main__":
    unittest.main()
