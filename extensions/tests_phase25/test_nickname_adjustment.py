"""Only three approved persona fragments may change; no services or network."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import tomlkit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/adjust_nickname_reaction_remote.py"
spec = importlib.util.spec_from_file_location("nickname_adjustment", SCRIPT)
nickname = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nickname)


class NicknameAdjustmentTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "bot_config.toml"
        self.backups = self.root / "backups"
        self.value = {"bot": {"nickname": "DeepSeek", "other": "stable-account"},
                      "personality": {
                          "personality": "固定基调和关系。" + nickname.REPLACEMENTS["personality"][0] + "其他身份不变。",
                          "reply_style": "原始语言风格。示范语气：" + nickname.REPLACEMENTS["reply_style"][0] + "；其他示例不变。",
                          "behavior_style": "原有互动习惯。" + nickname.REPLACEMENTS["behavior_style"][0] + "其他行为不变。",
                          "background": "unchanged"},
                      "features": {"quota": 20, "enabled": True},
                      "models": [{"name": "existing-binding", "tokens": 500}]}
        self.raw = tomlkit.dumps(self.value).encode()
        self.config.write_bytes(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_three_fragments_preserve_identity_relationship_and_all_other_values(self):
        updated, changed = nickname.prepare(self.raw)
        self.assertTrue(changed)
        result = tomlkit.parse(updated.decode()).unwrap()
        expected = copy.deepcopy(self.value)
        for field, (old, new) in nickname.REPLACEMENTS.items():
            expected["personality"][field] = expected["personality"][field].replace(old, new)
        self.assertEqual(result, expected)
        self.assertIn("好呀，我在呢～", result["personality"]["reply_style"])
        self.assertNotIn("嗯哼", result["personality"]["reply_style"])
        again, changed = nickname.prepare(updated)
        self.assertFalse(changed)
        self.assertEqual(again, updated)

    def test_default_preview_writes_nothing_and_does_not_print_other_persona_text(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            nickname.main(["--config", str(self.config), "--backup-root", str(self.backups)])
        result = json.loads(output.getvalue())
        self.assertFalse(result["applied"])
        self.assertTrue(result["other_fields_unchanged"])
        self.assertEqual(self.config.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())
        self.assertNotIn("固定基调和关系", output.getvalue())

    def test_missing_duplicate_and_partial_fragments_fail_without_changes(self):
        old, new = nickname.REPLACEMENTS["personality"]
        for replacement in ("different persona text", old + old, new):
            value = copy.deepcopy(self.value)
            value["personality"]["personality"] = replacement
            with self.assertRaises(ValueError):
                nickname.prepare(tomlkit.dumps(value).encode())
        self.assertEqual(self.config.read_bytes(), self.raw)

    def test_atomic_save_backups_original_and_protects_mode_owner(self):
        updated, _ = nickname.prepare(self.raw)
        owner = (os.getuid(), os.getgid())
        backup = nickname.save(self.config, self.backups, self.raw, updated, owner)
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertEqual(self.config.read_bytes(), updated)
        for path in (self.config, backup):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)

    def test_changed_file_and_failed_replace_leave_user_changes_or_original_intact(self):
        updated, _ = nickname.prepare(self.raw)
        self.config.write_bytes(self.raw + b"\n# concurrent change\n")
        with self.assertRaisesRegex(ValueError, "changed_since_preview"):
            nickname.save(self.config, self.backups, self.raw, updated, (os.getuid(), os.getgid()))
        self.assertFalse(self.backups.exists())
        self.config.write_bytes(self.raw)
        with (patch.object(nickname.os, "replace", side_effect=OSError("synthetic filesystem failure")),
              self.assertRaises(OSError)):
            nickname.save(self.config, self.backups, self.raw, updated, (os.getuid(), os.getgid()))
        self.assertEqual(self.config.read_bytes(), self.raw)
        self.assertEqual(next(self.backups.rglob("bot_config.toml")).read_bytes(), self.raw)
        self.assertFalse(list(self.root.glob(".nickname-*")))
