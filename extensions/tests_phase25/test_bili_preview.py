"""Preview and exact-draft sending with synthetic API/model only."""
import json
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import test_bili_unified as fixtures

from phase2.core import Denied

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import preview_bili_remote as preview


class PreviewTests(TestCase):
    setUp = fixtures.TestBiliUnified.setUp
    tearDown = fixtures.TestBiliUnified.tearDown
    new_engine = fixtures.TestBiliUnified.new_engine

    def test_preview_generates_once_saves_exact_text_and_never_reserves_or_sends(self):
        with patch.object(self.api, "candidates", wraps=self.api.candidates) as candidates, \
             patch.object(self.engine.sender, "deliver", side_effect=AssertionError("preview must never send")):
            value = preview.prepare(self.engine)
        self.assertEqual(value["status"], "preview_only_not_sent")
        self.assertEqual(candidates.call_args.args[0]["max_candidates"], 1)
        self.assertEqual(self.core.count, 1)
        self.assertFalse(self.api.calls)
        self.assertEqual(self.engine.s.db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
        saved = json.loads(Path(value["preview_file"]).read_text())
        self.assertEqual(preview.digest(saved), value["preview_id"])
        self.assertEqual(saved["text"], value["text"])
        self.assertNotIn("images", saved["target"])
        self.assertNotIn("text", saved["target"])
        self.assertIsNone(self.engine.s.get("browse_cycle"))

    def test_explicit_exact_preview_sends_saved_text_once_without_regeneration(self):
        value = preview.prepare(self.engine)
        result = preview.send_approved(self.engine, value["preview_id"])
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(self.api.calls[0][2], value["text"])
        self.assertEqual(self.core.count, 1)
        with self.assertRaisesRegex(Denied, "already_attempted"):
            preview.send_approved(self.engine, value["preview_id"])
        self.assertEqual(len(self.api.calls), 1)

    def test_tampered_saved_text_is_rejected_before_sender(self):
        value = preview.prepare(self.engine)
        path = Path(value["preview_file"])
        saved = json.loads(path.read_text())
        saved["text"] = "different content"
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(Denied, "fingerprint_mismatch"):
            preview.send_approved(self.engine, value["preview_id"])
        self.assertFalse(self.api.calls)

    def test_target_material_or_policy_change_requires_new_preview(self):
        value = preview.prepare(self.engine)
        original = self.api.video("1")
        original.text = "material has changed"
        with patch.object(self.api, "video", return_value=original), \
             self.assertRaisesRegex(Denied, "target_or_material_changed"):
            preview.send_approved(self.engine, value["preview_id"])
        self.c["paused"] = True
        self.config.write_text(json.dumps(self.c))
        with self.assertRaisesRegex(Denied, "policy_changed"):
            preview.send_approved(self.engine, value["preview_id"])
        self.assertFalse(self.api.calls)

    def test_unknown_delivery_and_receipt_sync_failure_never_allow_resend(self):
        value = preview.prepare(self.engine)
        self.api.failure = True
        self.core.fail_activity = True
        result = preview.send_approved(self.engine, value["preview_id"])
        self.assertEqual(result["result"]["status"], "unknown")
        with self.assertRaisesRegex(Denied, "already_attempted"):
            preview.send_approved(self.engine, value["preview_id"])
        self.assertEqual(len(self.api.calls), 1)

    def test_approved_preview_preserves_exact_mention_entity_without_regenerating(self):
        self.c["mentions"] = {"mode": "mutual_and_manual", "max_per_action": 1,
                              "manual_uids": [], "cache_seconds": 300}
        self.config.write_text(json.dumps(self.c))
        self.api.mutuals = lambda uid: [{"uid": "400", "name": "熟人"}]
        self.api.profile = lambda uid: {"uid": "400", "name": "熟人"}
        original = self.core
        def core(payload):
            result = original(payload)
            if payload["op"] == "decide":
                result["decision"]["mention_uids"] = ["400"]
            return result
        self.engine.unified_request = core
        value = preview.prepare(self.engine)
        self.assertEqual(value["mentions"], [{"uid": "400", "name": "熟人", "source": "mutual"}])
        result = preview.send_approved(self.engine, value["preview_id"])
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(self.api.calls[0][2].mentions[0]["uid"], "400")
        self.assertEqual(str(self.api.calls[0][2]), value["text"])
        self.assertEqual(self.core.count, 1)
