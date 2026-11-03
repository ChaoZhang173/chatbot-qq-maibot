"""compact_context.render must budget the text it actually returns (descriptive prefix included)."""
import unittest

from maibot_unified_core import compact_context

BUDGET = 5000          # service.qq_context: 4200 + max(0, 1800 - 1000)


class RenderBudgetTests(unittest.TestCase):
    def test_returned_text_never_exceeds_its_budget_and_keeps_rules_and_whole_records(self):
        rules = "规则" * 100
        for size in range(50, 500):
            value = {"persona_version": 1, "dynamic": {"name": "x"},
                     "facts": [{"text": "f" * size, "source": "s%d" % i} for i in range(40)]}
            out = compact_context.render(rules, value, BUDGET)
            self.assertLessEqual(len(out), BUDGET, size)             # was up to 15+ chars over before the fix
            self.assertTrue(out.startswith(rules + compact_context.RENDER_PREFIX))
            body = out[len(rules + compact_context.RENDER_PREFIX):]
            self.assertEqual(body.count('"text":"') , body.count('"source":"'))   # records are whole, never sliced


if __name__ == "__main__":
    unittest.main()
