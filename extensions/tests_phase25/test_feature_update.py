"""Incremental deployment preflight tests; no services or remote hosts touched."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import update_features_remote as update


class FeatureUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for folder, files in {
            "src/maibot_unified_core": ["__init__.py", "plugin.py", "service.py", "store.py", "host_adapter.py"],
            "src/phase2": ["__init__.py", "core.py", "api.py", "runner.py", "unified.py", "mentions.py", "engagement.py"],
            "scripts": ["configure_bili_features_remote.py", "operational_backup_remote.py"],
        }.items():
            (self.root / folder).mkdir(parents=True)
            for name in files: (self.root / folder / name).write_text("SYNTHETIC = True\n")
        (self.root / "src/maibot_unified_core/_manifest.json").write_text('{"id":"local.unified-core"}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_inventory_ignores_metadata_but_requires_new_modules_and_helpers(self):
        (self.root / "src/phase2/._runner.py").write_bytes(b"\x00AppleDouble")
        sources, manifest, helpers = update.sources_checked(self.root)
        self.assertEqual(len(sources), 12)
        self.assertEqual(len(helpers), 2)
        self.assertEqual(manifest.name, "_manifest.json")
        (self.root / "src/phase2/engagement.py").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete_staged_code"):
            update.sources_checked(self.root)

    def test_invalid_helper_syntax_and_symlinks_fail_before_stop(self):
        helper = self.root / "scripts/configure_bili_features_remote.py"
        helper.write_text("def broken(:")
        with self.assertRaises(SyntaxError): update.sources_checked(self.root)
        helper.unlink(); helper.symlink_to(self.root / "src/phase2/core.py")
        with self.assertRaisesRegex(ValueError, "invalid_staged_file"):
            update.sources_checked(self.root)

    def test_policy_preserves_false_flags_scopes_and_unrelated_reply_settings(self):
        original = {"learning_enabled": False, "reflection_enabled": False, "decisions_enabled": True,
                    "shared_groups": [["a", "b"]], "background_daily_limit": 20,
                    "qq_reply_policy": {"enabled": False, "casual_probability": .1, "future_setting": ["keep"]}}
        revised = update.qq_policy_update(original)
        self.assertFalse(revised["learning_enabled"])
        self.assertFalse(revised["reflection_enabled"])
        self.assertEqual(revised["shared_groups"], original["shared_groups"])
        self.assertEqual(revised["qq_reply_policy"], {"enabled": True, "casual_probability": .7, "future_setting": ["keep"]})
        self.assertEqual(original["qq_reply_policy"]["casual_probability"], .1)

    def test_snapshot_must_cover_every_discovered_database_with_integrity(self):
        expected = {"/db/" + str(i) for i in range(6)}
        rows = [{"source": p, "integrity": "ok"} for p in sorted(expected)]
        update.check_snapshot({"status": "complete", "databases": rows}, expected)
        for wrong in ({"status": "failed", "databases": rows},
                      {"status": "complete", "databases": rows[:-1]},
                      {"status": "complete", "databases": [{**r, "integrity": "bad"} for r in rows]}):
            with self.assertRaisesRegex(ValueError, "incomplete_feature_snapshot"):
                update.check_snapshot(wrong, expected)

    def test_atomic_failure_keeps_original_and_removes_partial_temporary(self):
        dest = self.root / "private.json"; dest.write_bytes(b"original")
        with patch.object(update.os, "chown", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                update.atomic(dest, b"replacement", os.getuid(), os.getgid(), 0o600)
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".private.json.phase25-update-*")), [])
        with patch.object(update.os, "chown"):
            update.atomic(dest, b"replacement", os.getuid(), os.getgid(), 0o600)
        self.assertEqual(dest.read_bytes(), b"replacement")
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__": unittest.main()
