"""Offline operational tests: temporary data and mocked service/ownership calls."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import operational_backup_remote as snapshots
import rollback_remote as rollback


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def database(path, rows=("synthetic-successful-action",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE actions(id TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO actions VALUES (?)", [(value,) for value in rows])
    return path


class TemporaryServer:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "server"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def path(self, value):
        return snapshots.rooted(self.root, value)


class SnapshotTests(TemporaryServer, unittest.TestCase):

    def test_appledouble_database_sidecars_are_not_opened_as_databases(self):
        database(self.path("/srv/bilibot/state/state.sqlite"))
        write(self.path("/srv/bilibot/state/._state.sqlite"), "binary resource fork, not SQLite")
        write(self.path("/srv/bilibot/state/__MACOSX/copied.sqlite"), "not a database")
        result = snapshots.create_snapshot(self.base / "sidecar-safe", root=self.root, minimum_free=0)
        self.assertEqual([row["source"] for row in result["databases"]], ["/srv/bilibot/state/state.sqlite"])

    def test_all_database_formats_and_live_wal_are_consistent(self):
        live = self.path("/srv/maibot/data/a-memorix/metadata.db")
        live.parent.mkdir(parents=True)
        connection = sqlite3.connect(live)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE knowledge(content TEXT)")
        connection.execute("INSERT INTO knowledge VALUES (?)", ("wal-committed" * 1000,))
        connection.commit()
        self.assertTrue(live.with_name(live.name + "-wal").exists())
        database(self.path("/srv/maibot/unified/state.sqlite3"))
        database(self.path("/srv/bilibot/state/state.sqlite"))
        vector = write(self.path("/srv/maibot/data/a-memorix/vectors/index.bin"), "mutable-vector")
        write(self.path("/srv/maibot/config/model_config.toml"), 'model="synthetic"\n')
        result = snapshots.create_snapshot(self.base / "saved", root=self.root, minimum_free=0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["databases"]), 3)
        source = next(row for row in result["databases"] if row["source"].endswith("metadata.db"))
        with sqlite3.connect(self.base / "saved" / source["snapshot"]) as copy:
            self.assertEqual(copy.execute("SELECT content FROM knowledge").fetchone()[0], "wal-committed" * 1000)
        self.assertIn(str(vector.relative_to(self.root)), result["omitted_live_memory_files"])
        self.assertFalse(list((self.base / "saved").rglob("index.bin")))
        self.assertEqual((self.base / "saved/manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.base / "saved").stat().st_mode & 0o777, 0o700)

    def test_size_limit_counts_wal_pages_before_snapshot(self):
        live = self.path("/srv/maibot/data/test.db")
        live.parent.mkdir(parents=True)
        connection = sqlite3.connect(live)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE payload(content BLOB)")
        connection.execute("INSERT INTO payload VALUES (?)", (b"x" * 100000,))
        connection.commit()
        self.assertGreater(snapshots.database_size(live), live.stat().st_size)
        with self.assertRaisesRegex(ValueError, "size_limit"):
            snapshots.create_snapshot(self.base / "oversize", root=self.root, minimum_free=0, maximum_state=20000)
        self.assertFalse((self.base / "oversize").exists())

    def test_symlink_state_root_and_configuration_fail_closed(self):
        actual = self.base / "outside"
        actual.mkdir()
        link = self.path("/srv/maibot/data")
        link.parent.mkdir(parents=True)
        link.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "symlink_state_root"):
            snapshots.create_snapshot(self.base / "bad", root=self.root, minimum_free=0)
        link.unlink()
        config = self.path("/srv/maibot/config/model_config.toml")
        config.parent.mkdir(parents=True)
        config.symlink_to(write(self.base / "outside.toml", "private_synthetic=true"))
        with self.assertRaisesRegex(ValueError, "symlink_configuration"):
            snapshots.create_snapshot(self.base / "failed", root=self.root, minimum_free=0)
        self.assertEqual(json.loads((self.base / "failed/manifest.json").read_text())["status"], "failed")

    def test_rotation_only_removes_completed_owned_daily_snapshots(self):
        for name, created, state, marker in [
            ("daily-unified-old", 1, "complete", snapshots.MARKER),
            ("daily-unified-new", 2, "complete", snapshots.MARKER),
            ("daily-unified-failed", 0, "failed", snapshots.MARKER),
            ("daily-unified-foreign", 0, "complete", "other"),
            ("before-unified-old", 0, "complete", snapshots.MARKER),
        ]:
            write(self.base / name / "manifest.json", json.dumps({"created_at": created, "status": state, "generator": marker}))
        self.assertEqual(snapshots.rotate(self.base, 1), 1)
        self.assertFalse((self.base / "daily-unified-old").exists())
        for name in ("daily-unified-new", "daily-unified-failed", "daily-unified-foreign", "before-unified-old"):
            self.assertTrue((self.base / name).exists())


class RollbackTests(TemporaryServer, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.original = b"def register(cap_service):\n    return None\n"
        self.current = self.original.decode().replace("    return None\n", rollback.INSERTION + "    return None\n").encode()
        write(self.path(rollback.REGISTRY), self.current.decode())
        write(self.path(rollback.REGISTRY).with_name("unified_memory.py"), "ADAPTER = True\n")
        self.backup = self.base / "baseline"
        write(self.backup / "maibot-config/bot_config.toml", "baseline_bot=true\n")
        write(self.backup / "maibot-config/model_config.toml", "baseline_model=true\n")
        write(self.backup / "bilibot-code/phase2/runner.py", "BASELINE_RUNNER = True\n")
        baseline_db = database(self.backup / "bilibot.sqlite", ("older-action",))
        write(self.backup / "manifest.json", json.dumps({
            "databases": [{"snapshot": "bilibot.sqlite", "sha256": snapshots.digest(baseline_db)}],
            "service_states": {"maibot": "active", "bilibot": "active"},
        }))
        write(self.path("/srv/maibot/config/bot_config.toml"), "new_bot=true\n")
        write(self.path("/srv/maibot/config/model_config.toml"), "new_model=true\n")
        write(self.path("/srv/maibot/plugins/unified_core/plugin.py"), "NEW_PLUGIN = True\n")
        write(self.path("/opt/bilibot/app/phase2/runner.py"), "NEW_RUNNER = True\n")
        write(self.path("/opt/bilibot/app/phase2/unified.py"), "NEW_CLIENT = True\n")
        write(self.path("/srv/bilibot/private/config.json"), '{"public_switch":"unchanged"}')
        write(self.path("/srv/bilibot/private/PAUSE"), "paused")
        database(self.path("/srv/bilibot/state/state.sqlite"), ("old-success", "new-success-never-resend"))
        database(self.path("/srv/maibot/unified/state.sqlite"), ("new-persona-and-source",))
        self.hash_patch = patch.object(rollback, "BASE_SHA256", hashlib.sha256(self.original).hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def test_preflight_is_read_only_and_rejects_unknown_host_change(self):
        plan = rollback.preflight(self.backup, root=self.root)
        self.assertEqual(plan["host_original"], self.original)
        self.assertEqual(self.path(rollback.REGISTRY).read_bytes(), self.current)
        write(self.path(rollback.REGISTRY), self.current.decode() + "UNRELATED = True\n")
        with self.assertRaisesRegex(ValueError, "unreviewed_changes"):
            rollback.preflight(self.backup, root=self.root)

    def test_corrupt_baseline_snapshot_blocks_rollback(self):
        (self.backup / "bilibot.sqlite").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            rollback.preflight(self.backup, root=self.root)

    def test_utf8_bom_is_preserved_and_compiles_as_raw_bytes(self):
        bom = b"\xef\xbb\xbf"
        self.path(rollback.REGISTRY).write_bytes(bom + self.current)
        (self.backup / "bilibot-code/phase2/runner.py").write_bytes(bom + b"BASELINE_RUNNER = True\n")
        with patch.object(rollback, "BASE_SHA256", hashlib.sha256(bom + self.original).hexdigest()):
            plan = rollback.preflight(self.backup, root=self.root)
        self.assertEqual(plan["host_original"], bom + self.original)
        self.assertTrue(plan["bili_files"][0].read_bytes().startswith(bom))

    def test_appledouble_is_ignored_but_real_source_syntax_still_blocks(self):
        directory = self.backup / "bilibot-code/phase2"
        (directory / "._runner.py").write_bytes(b"\x00\x05\x16\x07binary AppleDouble")
        plan = rollback.preflight(self.backup, root=self.root)
        self.assertEqual([p.name for p in plan["bili_files"]], ["runner.py"])
        self.assertTrue((directory / "._runner.py").exists())
        (directory / "runner.py").write_text("def invalid(:\n")
        with self.assertRaises(SyntaxError):
            rollback.preflight(self.backup, root=self.root)

    def test_rollback_does_not_restore_metadata_as_code(self):
        (self.backup / "bilibot-code/phase2/._runner.py").write_bytes(b"\x00AppleDouble")
        write(self.path("/opt/bilibot/app/phase2/._unified.py"), "resource fork")
        write(self.backup / "maibot-plugins/unified_core/plugin.py", "OLD_PLUGIN = True\n")
        write(self.backup / "maibot-plugins/unified_core/._plugin.py", "resource fork")
        result, _ = self.run_rollback(rollback.preflight(self.backup, root=self.root))
        self.assertFalse(self.path("/opt/bilibot/app/phase2/._runner.py").exists())
        self.assertFalse(self.path("/srv/maibot/plugins/unified_core/._plugin.py").exists())
        self.assertTrue(self.path("/srv/maibot/plugins/unified_core/plugin.py").exists())
        self.assertFalse((Path(result["safety_snapshot"]) / "changed-code/bili-phase2/._unified.py").exists())

    def run_rollback(self, plan, fail_restore=False):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0)
        with patch.object(rollback.os, "geteuid", return_value=0), \
             patch.object(rollback.os, "umask"), \
             patch.object(rollback.os, "chown"), \
             patch.object(rollback.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())), \
             patch.object(rollback.subprocess, "run", side_effect=run), \
             patch.object(rollback.subprocess, "check_output", return_value="inactive\n"):
            if fail_restore:
                with patch.object(rollback, "atomic_restore", side_effect=RuntimeError("synthetic_restore_failure")), \
                     self.assertRaisesRegex(RuntimeError, "synthetic_restore_failure"):
                    rollback.apply(plan, root=self.root)
                return None, calls
            return rollback.apply(plan, root=self.root), calls

    def test_full_rollback_preserves_new_success_ledgers_and_unified_data(self):
        plan = rollback.preflight(self.backup, root=self.root)
        live = self.path("/srv/bilibot/state/state.sqlite")
        unified = self.path("/srv/maibot/unified/state.sqlite")
        before = snapshots.digest(live), snapshots.digest(unified)
        result, calls = self.run_rollback(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(before, (snapshots.digest(live), snapshots.digest(unified)))
        with sqlite3.connect(live) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM actions").fetchone()[0], 2)
        self.assertEqual(self.path(rollback.REGISTRY).read_bytes(), self.original)
        self.assertIn("baseline_bot", self.path("/srv/maibot/config/bot_config.toml").read_text())
        self.assertIn("BASELINE_RUNNER", self.path("/opt/bilibot/app/phase2/runner.py").read_text())
        self.assertFalse(self.path("/srv/maibot/plugins/unified_core").exists())
        self.assertFalse(self.path("/opt/bilibot/app/phase2/unified.py").exists())
        self.assertTrue(self.path("/srv/bilibot/private/PAUSE").exists())
        self.assertIn("unchanged", self.path("/srv/bilibot/private/config.json").read_text())
        self.assertEqual(calls[:2], [["systemctl", "stop", "bilibot"], ["systemctl", "stop", "maibot"]])
        self.assertEqual([call for call in calls if call[1] == "start"], [["systemctl", "start", "maibot"], ["systemctl", "start", "bilibot"]])
        saved = Path(result["safety_snapshot"])
        self.assertTrue((saved / "changed-code/disabled-unified-core/plugin.py").is_file())
        self.assertTrue((saved / "databases/srv/maibot/unified/state.sqlite").is_file())

    def test_partial_restore_failure_keeps_both_services_stopped(self):
        result, calls = self.run_rollback(rollback.preflight(self.backup, root=self.root), fail_restore=True)
        self.assertIsNone(result)
        self.assertFalse([call for call in calls if call[1] == "start"])
        self.assertEqual(calls[-2:], [["systemctl", "stop", "bilibot"], ["systemctl", "stop", "maibot"]])
        reports = list(self.path("/srv/maibot/backups").glob("before-rollback-*/rollback-report.json"))
        self.assertEqual(json.loads(reports[0].read_text())["status"], "failed_keep_services_stopped")


if __name__ == "__main__":
    unittest.main()
