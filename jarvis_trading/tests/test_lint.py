import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import lint as L
from jarvis_trading import strategies as S


class LintTests(unittest.TestCase):
    def test_every_research_template_and_family_is_clean(self):
        for template in S.research_templates():
            _, source, _ = S.generate(template)
            self.assertEqual(L.future_reference_findings(source), [], template)

    def test_every_control_is_caught(self):
        for template in S.CONTROL_TEMPLATES:
            if S.is_ml(template):
                continue  # the ML control is text-only config; it is caught by the dynamic feature-causality test instead
            _, source, _ = S.generate(template)
            self.assertTrue(L.future_reference_findings(source), template)

    def test_detects_common_future_constructs(self):
        for bad in ('df["a"] = df["close"].shift(-2)', 'x = s.rolling(5, center=True).mean()', 'y = s.bfill()',
                    'z = s.interpolate()', 'q = df.iloc[i + 1]', 'future_close = df["close"]'):
            self.assertTrue(L.future_reference_findings(bad), bad)

    def test_ignores_comments_and_positive_shifts(self):
        self.assertEqual(L.future_reference_findings('x = s.shift(1)  # not shift(-1)'), [])
        self.assertEqual(L.future_reference_findings('# s.shift(-1) in a comment'), [])


if __name__ == "__main__":
    unittest.main()
