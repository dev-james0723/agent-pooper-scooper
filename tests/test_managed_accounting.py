"""Read-only accounting from real broker facts; no user roots or live tools."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import authority


class ManagedAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aps-account-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / "store"
        self.now = time.time() - 3600
        patch = mock.patch.object(authority, "_now", side_effect=lambda: self.now)
        patch.start(); self.addCleanup(patch.stop)
        self.owner = authority.ManagedAuthority.create(self.path)
        self.creds = self.owner.register("task", "producer")

    def write(self, data=b"accounted fixture"):
        return self.owner.producer_call(self.creds, "write", {"data": data, "artifact_class": "temp", "disposable": True})

    def test_completed_creation_current_identity_and_no_mutation(self):
        artifact = self.write(); other = self.owner.register("other", "consumer")
        self.owner.producer_call(other, "reference", {"artifact_id": artifact})
        self.owner.producer_call(other, "lease", {"artifact_id": artifact, "expires_at": "2000-01-01T00:00:00Z"})
        before = self.owner.snapshot("task")
        with mock.patch.object(self.owner, "_commit", side_effect=AssertionError("accounting must not commit")):
            result = self.owner.accounting_snapshot("task")
        item = result["artifacts"][0]
        self.assertTrue(item["creation_verified"])
        self.assertEqual(item["created_bytes"], len(b"accounted fixture"))
        self.assertEqual(item["current_status"], "present_verified")
        self.assertEqual(item["current_bytes"], item["created_bytes"])
        self.assertEqual((item["reference_count"], item["lease_count"]), (1, 1))
        self.assertEqual(result["outcomes"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(self.owner.snapshot("task"), before)
        raw = json.dumps(result)
        for private in (str(self.path), self.owner.owner_key, self.creds["token"], "accounted fixture"):
            self.assertFalse(private in raw, "private data escaped accounting whitelist")

    def test_confirmed_unlink_is_bound_and_history_not_confused_with_current(self):
        artifact = self.write(b"x" * 17)
        self.owner.producer_call(self.creds, "terminal", {"status": "completed"}); self.now += 3600
        snapshot = self.owner.snapshot("task"); item = snapshot["artifacts"][0]
        grant = self.owner.approve("task", "a" * 64, [artifact], snapshot["revision"], "collect", 30)
        identity = {key: item[key] for key in authority.IDENTITY_FIELDS}
        with self.owner.transaction("task", snapshot["revision"], time.monotonic() + 1) as fence:
            intent = fence.prepare("collect", "a" * 64, artifact, identity, grant["approval_id"])
            os.unlink(item["name"], dir_fd=fence.root_fd); os.fsync(fence.root_fd)
            fence.finish(intent, "unlinked", {"identity": identity})
        result = self.owner.accounting_snapshot("task")
        self.assertEqual(result["artifacts"][0]["created_bytes"], 17)
        self.assertEqual(result["artifacts"][0]["current_status"], "missing")
        self.assertIsNone(result["artifacts"][0]["current_bytes"])
        self.assertEqual(len(result["outcomes"]), 1)
        self.assertTrue(result["outcomes"][0]["binding_verified"])
        self.assertEqual(result["outcomes"][0]["removed_bytes"], 17)
        self.assertTrue(result["complete"])

    def test_unmaterialized_reconciled_create_stays_unknown(self):
        real = os.open
        def denied(path, flags, *args, **kwargs):
            if isinstance(path, str) and path.endswith(".blob") and flags & os.O_CREAT:
                raise PermissionError("fixture before create")
            return real(path, flags, *args, **kwargs)
        with mock.patch.object(authority.os, "open", side_effect=denied):
            with self.assertRaises(authority.AuthorityError): self.write()
        pending = self.owner.pending_operations("task")[0]
        result = self.owner.accounting_snapshot("task")
        self.assertTrue(result["recovery_required"])
        self.assertFalse(result["artifacts"][0]["creation_verified"])
        self.assertIsNone(result["artifacts"][0]["created_bytes"])
        self.owner.reconcile(pending["operation_id"], pending["artifact_id"])
        result = self.owner.accounting_snapshot("task")
        self.assertFalse(result["artifacts"][0]["creation_verified"])
        self.assertIsNone(result["artifacts"][0]["identity"])
        self.assertIsNone(result["artifacts"][0]["created_bytes"])
        self.assertEqual(result["outcomes"], [])  # Producer recovery is NOT retirement.
        self.assertFalse(result["complete"])

    def test_current_change_is_unknown_without_losing_provable_creation(self):
        self.write()
        item = self.owner.snapshot("task")["artifacts"][0]
        target = self.path / "artifacts" / item["name"]
        target.write_bytes(b"fixture raw external change")
        result = self.owner.accounting_snapshot("task")
        self.assertTrue(result["artifacts"][0]["creation_verified"])
        self.assertEqual(result["artifacts"][0]["current_status"], "changed")
        self.assertIsNone(result["artifacts"][0]["current_bytes"])
        self.assertFalse(result["complete"])

    def test_selected_task_only_and_snapshot_is_detached(self):
        artifact = self.write()
        other = self.owner.register("other", "producer")
        self.owner.producer_call(other, "write", {"data": b"foreign", "artifact_class": "temp", "disposable": True})
        result = self.owner.accounting_snapshot("task")
        self.assertEqual([a["artifact_id"] for a in result["artifacts"]], [artifact])
        result["artifacts"][0]["created_bytes"] = 999
        self.assertEqual(self.owner.accounting_snapshot("task")["artifacts"][0]["created_bytes"], len(b"accounted fixture"))


if __name__ == "__main__":
    unittest.main()
