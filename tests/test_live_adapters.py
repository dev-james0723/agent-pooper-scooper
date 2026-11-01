"""Hermetic native-boundary and durable notification tests; no live publishing."""
import copy
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import authority, live_adapters as live


def partial_stdio_response(queue):
    rfd, wfd = os.pipe()
    reader = os.fdopen(rfd, "r")
    try:
        os.write(wfd, b"{")  # Never supply newline/EOF: first-byte readiness is insufficient.
        transport = live.StdioToolTransport(reader=reader, writer=io.StringIO(), timeout=.1)
        try:
            transport("get_mission", {"missionId": "fixture", "metricsOnly": False})
            queue.put("BAD")
        except live.LiveAdapterError as exc:
            queue.put(exc.code)
    finally:
        os.close(wfd); reader.close()


def full_stdio_request(queue):
    rfd, wfd = os.pipe()
    rr, rw = os.pipe()
    writer = os.fdopen(wfd, "w")
    reader = os.fdopen(rr, "r")
    try:
        os.set_blocking(wfd, False)
        try:
            while True: os.write(wfd, b"x" * 4096)
        except BlockingIOError: pass
        os.set_blocking(wfd, True)
        try:
            live.StdioToolTransport(reader=reader, writer=writer, timeout=.1)("get_mission", {})
            queue.put("BAD")
        except live.LiveAdapterError as exc: queue.put(exc.code)
    finally:
        for fd in (rfd, wfd, rr, rw):
            try: os.close(fd)
            except OSError: pass
        for stream in (writer, reader):
            try: stream.close()
            except OSError: pass


class LiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aps-live-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.owner = authority.ManagedAuthority.create(self.base / "store")
        self.calls = []
        self.report = {"removed": {"count": 2, "bytes": 17}, "eligible": {"count": 1, "apparent_bytes": 7},
                       "reclaimed": {"bytes": None}, "private": str(self.base), "task_id": "private-task"}

    def notifier(self, call):
        return live.KynloNotificationAdapter(call, self.owner, "fixture-device", "fixture-mission")

    def ack(self, name, params):
        self.calls.append((name, params))
        return {"content": [{"type": "text", "text": "Notification sent."}]}

    def test_notification_projection_is_redacted_and_ack_not_user_seen(self):
        receipt = self.notifier(self.ack).send("notice", self.report)
        self.assertEqual(receipt["status"], "provider_acknowledged")
        self.assertFalse(receipt["user_seen_verified"])
        self.assertEqual(self.calls[0][0], "notify")
        params = self.calls[0][1]
        self.assertEqual(params["deviceId"], "fixture-device")
        self.assertNotIn(str(self.base), params["message"])
        self.assertNotIn("private-task", params["message"])
        self.assertIn("17", params["message"])
        self.assertNotIn("fixture-device", json.dumps(receipt))

    def test_notification_same_key_does_not_dispatch_again_after_reopen(self):
        first = self.notifier(self.ack).send("notice", self.report)
        owner = authority.ManagedAuthority(self.owner.path, owner_key=self.owner.owner_key)
        second = live.KynloNotificationAdapter(self.ack, owner, "fixture-device", "fixture-mission").send("notice", self.report)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_changed_notice_or_target_same_key_refuses(self):
        self.notifier(self.ack).send("notice", self.report)
        changed = copy.deepcopy(self.report); changed["removed"]["bytes"] += 1
        with self.assertRaises(live.LiveAdapterError): self.notifier(self.ack).send("notice", changed)
        with self.assertRaises(live.LiveAdapterError):
            live.KynloNotificationAdapter(self.ack, self.owner, "other-device", "fixture-mission").send("notice", self.report)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_delivery_persists_and_never_blindly_retries(self):
        def unknown(name, params):
            self.calls.append((name, params))
            raise TimeoutError("private provider diagnostic " + str(self.base))
        first = self.notifier(unknown).send("notice", self.report)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(self.notifier(self.ack).send("notice", self.report), first)
        self.assertEqual(len(self.calls), 1)
        result = self.notifier(self.ack).reconcile("notice")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "call_id_unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_intent_is_durable_before_transport_and_crash_never_resends(self):
        def crash(name, params):
            with self.owner._locked() as (conn, state, root, deadline):
                saved = json.loads(conn.execute("SELECT body FROM notifications WHERE id='notice'").fetchone()[0])
                self.assertEqual(saved["status"], "pending")
            raise KeyboardInterrupt("fixture process interruption")
        with self.assertRaises(KeyboardInterrupt): self.notifier(crash).send("notice", self.report)
        result = self.notifier(self.ack).send("notice", self.report)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.calls, [])

    def test_call_id_reconciliation_is_read_only_and_identity_bound(self):
        def call(name, params):
            self.calls.append((name, params))
            if name == "notify":
                return {"isError": True, "structuredContent": {"callId": "c_fixture", "status": "unknown"}}
            return {"structuredContent": {"callId": "c_fixture", "status": "completed", "outcome": "success"}}
        notifier = self.notifier(call)
        self.assertEqual(notifier.send("notice", self.report)["status"], "unknown")
        self.assertEqual(notifier.reconcile("notice")["status"], "provider_acknowledged")
        self.assertEqual([x[0] for x in self.calls], ["notify", "get_call_status"])
        self.assertEqual(self.calls[-1][1], {"callId": "c_fixture"})

    def test_missing_transport_and_invalid_accounting_refuse_before_effect(self):
        with self.assertRaises(live.LiveAdapterError): self.notifier(None).send("notice", self.report)
        for value in (-1, True, "17"):
            bad = copy.deepcopy(self.report); bad["removed"]["bytes"] = value
            with self.assertRaises(live.LiveAdapterError): self.notifier(self.ack).send("notice", bad)
        self.assertEqual(self.calls, [])

    def test_unknown_provider_shape_does_not_invent_ack(self):
        result = self.notifier(lambda name, params: {"content": [{"type": "text", "text": "maybe completed"}]}).send("notice", self.report)
        self.assertEqual(result["status"], "unknown")

    def test_malformed_ack_is_unknown_not_an_exception(self):
        result = self.notifier(lambda name, params: {"content": [{"type": "text", "text": {"private": "payload"}}]}).send("notice", self.report)
        self.assertEqual(result["status"], "unknown")

    def test_post_dispatch_ledger_failure_is_unknown_and_does_not_resend(self):
        from unittest import mock
        notifier = self.notifier(self.ack)
        with mock.patch.object(notifier, "_update", side_effect=authority.AuthorityError("authority_storage_unavailable")):
            receipt = notifier.send("notice", self.report)
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["reason"], "delivery_record_unconfirmed")
        self.assertEqual(self.notifier(self.ack).send("notice", self.report)["status"], "unknown")
        self.assertEqual(len(self.calls), 1)

    def test_stdio_partial_response_obeys_deadline_after_first_byte(self):
        ctx = multiprocessing.get_context("spawn"); queue = ctx.Queue()
        child = ctx.Process(target=partial_stdio_response, args=(queue,)); child.start()
        try:
            child.join(1.5)
            self.assertFalse(child.is_alive(), "bridge blocked after first byte without newline")
            self.assertEqual(queue.get(timeout=.5), "transport_outcome_unknown")
        finally:
            if child.is_alive(): child.terminate(); child.join(1)

    def test_full_request_pipe_obeys_same_total_deadline(self):
        ctx = multiprocessing.get_context("spawn"); queue = ctx.Queue()
        child = ctx.Process(target=full_stdio_request, args=(queue,)); child.start()
        try:
            child.join(1.5)
            self.assertFalse(child.is_alive(), "full request pipe blocked before timeout")
            self.assertEqual(queue.get(timeout=.5), "transport_outcome_unknown")
        finally:
            if child.is_alive(): child.terminate(); child.join(1)

    def test_matching_identity_malformed_states_are_unknown_and_private(self):
        for state in (["private-state", str(self.base)], {"private": str(self.base)}):
            answers = [
                live.AoSessionAdapter("fixture-session", "fixture-project", runner=lambda *a, **k: {"session": {"id": "fixture-session", "projectId": "fixture-project", "status": state}}).observe,
                live.KynloStateAdapter(lambda *a: {"structuredContent": {"id": "fixture-mission", "deviceId": "fixture-device", "state": state}}, "fixture-device", "fixture-mission").observe_mission,
                live.KynloStateAdapter(lambda *a: {"structuredContent": {"session": {"handle": "fixture-handle", "deviceId": "fixture-device", "state": state}}}, "fixture-device", "fixture-mission", handle="fixture-handle").observe_agent,
            ]
            for observe in answers:
                with self.subTest(state_type=type(state).__name__):
                    result = observe()
                    self.assertEqual(result["status"], "unknown")
                    self.assertFalse(result["can_authorize_cleanup"])
                    self.assertFalse(str(self.base) in json.dumps(result))

    def test_mission_observation_matches_actual_top_level_contract_and_is_redacted(self):
        def call(name, args):
            self.calls.append((name, args))
            return {"structuredContent": {"id": "fixture-mission", "deviceId": "fixture-device", "state": "running", "jobs": [{"private": str(self.base)}]}}
        result = live.KynloStateAdapter(call, "fixture-device", "fixture-mission").observe_mission()
        self.assertTrue(result["identity_verified"])
        self.assertFalse(result["can_authorize_cleanup"])
        self.assertNotIn(str(self.base), json.dumps(result))
        self.assertEqual(self.calls, [("get_mission", {"missionId": "fixture-mission", "metricsOnly": False})])

    def test_command_transport_errors_do_not_retain_private_context(self):
        program = self.base / "failed-bridge.py"
        program.write_text("import sys\nsys.stderr.write('private backend detail')\nsys.exit(1)\n")
        transport = live.CommandToolTransport([sys.executable, str(program)])
        with self.assertRaises(live.LiveAdapterError) as caught: transport("get_mission", {})
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("private backend", str(caught.exception))

    def test_ao_adapter_requests_exact_session_and_parses_only_safe_fields(self):
        def runner(argv, *, timeout):
            self.calls.append((argv, timeout))
            return {"session": {"id": "fixture-session", "projectId": "fixture-project", "status": "working", "harness": "codex",
                                "branch": str(self.base), "displayName": "private title", "isTerminated": False}}
        observed = live.AoSessionAdapter("fixture-session", "fixture-project", runner=runner).observe()
        self.assertEqual(self.calls[0][0], ["ao", "session", "get", "fixture-session", "--json"])
        self.assertEqual(observed["status"], "active")
        self.assertFalse(observed["can_authorize_cleanup"])
        self.assertNotIn(str(self.base), json.dumps(observed))
        self.assertNotIn("private title", json.dumps(observed))

    def test_ao_unknown_outage_identity_mismatch_are_not_authority(self):
        for answer in ({"session": {"id": "wrong", "projectId": "fixture-project", "status": "working"}},
                       {"session": {"id": "fixture-session", "projectId": "fixture-project", "status": "idle"}}, {}):
            result = live.AoSessionAdapter("fixture-session", "fixture-project", runner=lambda *a, **k: answer).observe()
            self.assertFalse(result["can_authorize_cleanup"])
            self.assertNotEqual(result["status"], "completed")

    def test_kynlo_agent_requires_verified_handle_and_exact_device(self):
        def call(name, params):
            self.calls.append((name, params))
            return {"structuredContent": {"session": {"handle": "fixture-handle", "deviceId": "fixture-device", "state": "running"}}}
        adapter = live.KynloStateAdapter(call, "fixture-device", "fixture-mission")
        self.assertEqual(adapter.observe_agent()["reason"], "agent_handle_unverified")
        self.assertEqual(self.calls, [])
        adapter = live.KynloStateAdapter(call, "fixture-device", "fixture-mission", handle="fixture-handle")
        observed = adapter.observe_agent()
        self.assertFalse(observed["can_authorize_cleanup"])
        self.assertEqual(self.calls, [("get_agent_session", {"deviceId": "fixture-device", "handle": "fixture-handle"})])

    def test_command_transport_is_explicit_argv_json_stdin_not_shell(self):
        program = self.base / "bridge.py"
        program.write_text("import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'structuredContent':{'received':r['tool'],'arguments':r['arguments']}}))\n")
        transport = live.CommandToolTransport([sys.executable, str(program)])
        result = transport("get_mission", {"missionId": "fixture-mission", "metricsOnly": False})
        self.assertEqual(result["structuredContent"]["received"], "get_mission")
        with self.assertRaises(live.LiveAdapterError): transport("invented_tool", {})


if __name__ == "__main__":
    unittest.main()
