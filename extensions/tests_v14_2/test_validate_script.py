"""The coordinator's validation command runs offline and reports selection versus history from a state database read-only."""
import importlib.util
import unittest

from v14_2_support import Harness, ROOT, HOUR


def load_script():
    spec = importlib.util.spec_from_file_location('v14_2_validate', ROOT / 'scripts' / 'v14_2_validate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidateScriptTests(unittest.TestCase):
    def test_config_and_schema_checks_pass(self):
        script = load_script()
        script.CHECKS.clear()
        script.config_checks()
        script.schema_checks()
        failed = [c for c in script.CHECKS if not c['ok']]
        self.assertEqual(failed, [])
        self.assertGreaterEqual(len(script.CHECKS), 15)

    def test_state_report_distinguishes_history_from_the_selected_recent_sample(self):
        h = Harness(self)
        script = load_script()
        base = h.clock()
        h.bulk_events(300, 'old', 'a', base - 8 * 86400)
        before = script.state_report(h.root / 'state.sqlite')
        self.assertEqual(before['pending_raw_events_total'], 300)
        self.assertIn('note', before)                                 # not activated: all of it is history
        h.clock.advance(60)
        h.activate()
        h.state.db.execute("INSERT OR REPLACE INTO meta VALUES('daily_activation_at',?)", (str(h.clock() - 1),))
        after = script.state_report(h.root / 'state.sqlite')
        self.assertEqual(after['historical_raw_not_a_learning_debt'], 300)
        self.assertEqual(after['recent_pending_since_cutover_within_24h'], 0)
