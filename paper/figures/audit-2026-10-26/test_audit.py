"""Offline regression checks for denominator mistakes and boundary uncertainty."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('chart_audit', Path(__file__).with_name('build.py'))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def test_zero_observed_is_not_zero_uncertainty(self):
        low, high = audit.wilson(0, 18)
        self.assertAlmostEqual(low, 0)
        self.assertGreater(high, .17)
        self.assertLess(high, .18)

    def test_invalid_counts_are_rejected(self):
        for k, n in [(1, 0), (-1, 10), (11, 10)]:
            with self.assertRaises(ValueError):
                audit.wilson(k, n)

    def test_complement_reverses_interval_endpoints(self):
        low, high = audit.wilson(243, 999)
        low_d, high_d = audit.wilson(756, 999)
        self.assertAlmostEqual(low_d, 1-high)
        self.assertAlmostEqual(high_d, 1-low)

    def test_saved_exposure_denominators_are_not_interchangeable(self):
        summaries, _ = audit.load()
        d = summaries['GPT-4o-mini']['tables']['llmail']['C3']
        self.assertEqual((d['fpr']['successes'], d['fpr']['total']), (7, 500))
        self.assertEqual((d['guard_fpr_blocked']['successes'], d['guard_fpr_blocked']['total']), (7, 195))
        self.assertEqual((d['guard_fpr']['successes'], d['guard_fpr']['total']), (8, 195))
        self.assertNotEqual(audit.rate(d['fpr']), audit.rate(d['guard_fpr_blocked']))

    def test_full_saved_artifact_audit(self):
        summaries, manifests = audit.load()
        rows = audit.audit(summaries, manifests)
        self.assertEqual(len(rows), 108)


if __name__ == '__main__':
    unittest.main()
