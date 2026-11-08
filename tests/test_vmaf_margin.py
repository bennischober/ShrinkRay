"""The effective VMAF target: absolute first, then a margin below the ceiling.

The VMAF model does not score identical frames as 100: its maximum is
content-dependent (measured 97.4-99.9 across four shapes, issue #584), so an
absolute target is unreachable on some content. The search tries the ABSOLUTE target
first - a source that can meet it keeps it, and no ceiling is measured - and only a
source that would otherwise be skipped pays for the ceiling and aims at
`min(target_vmaf, ceiling - vmaf_margin)`. `vmaf_margin` 0 disables that second
phase entirely.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine import search as engine_search


class TestEffectiveTarget(unittest.TestCase):
    def _search(self, curve, ceiling, margin, target=96.0):
        seen: list[int] = []

        def fake_probe(samples, crf, *args, **kwargs):
            seen.append(crf)
            return curve[crf]

        with (
            patch.object(engine_search, "probe_crf", side_effect=fake_probe),
            patch.object(engine_search, "measure_vmaf", return_value=ceiling) as measured,
        ):
            chosen, achieved, probes, effective = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=target,
                crf_range=(30, 36),
                vmaf_margin=margin,
                samples=[(Path("sample.mkv"), 0.0)],
            )
        return chosen, achieved, probes, effective, seen, measured

    def test_a_zero_margin_keeps_the_absolute_target(self):
        """margin 0 = the fixed absolute target, and no ceiling measurement."""
        chosen, _achieved, _probes, effective, _seen, measured = self._search(
            {30: 96.2, 31: 95.0, 32: 94.0, 33: 93.0, 34: 92.0, 35: 91.0},
            ceiling=98.46,
            margin=0.0,
        )
        self.assertEqual(effective, 96.0)
        self.assertEqual(chosen, 30)
        measured.assert_not_called()

    def test_a_reachable_source_never_pays_for_the_ceiling(self):
        """A source that meets the absolute target keeps it (issue #584)."""
        chosen, _achieved, _probes, effective, _seen, measured = self._search(
            {30: 96.2, 31: 95.0, 32: 94.0, 33: 93.0, 34: 92.0, 35: 91.0},
            ceiling=98.46,
            margin=3.0,
        )
        self.assertEqual(effective, 96.0, "a reachable source must keep the absolute target")
        self.assertEqual(chosen, 30)
        measured.assert_not_called()

    def test_an_unreachable_source_aims_below_its_ceiling(self):
        """The second phase: ceiling 98.46, margin 3 -> 95.46, and the search resumes.

        The curve is monotonic, as `bisect_crf` requires: the floor (95.9) misses the
        absolute 96 but meets the relaxed 95.46, so the largest CRF meeting 95.46
        (32 at 95.5) must be chosen - not the floor, which is the largest output.
        """
        chosen, achieved, probes, effective, seen, measured = self._search(
            {30: 95.9, 31: 95.7, 32: 95.5, 33: 95.3, 34: 95.0, 35: 94.0},
            ceiling=98.46,
            margin=3.0,
        )
        self.assertAlmostEqual(effective, 95.46)
        measured.assert_called()
        self.assertEqual(chosen, 32, "the relaxed search must leave the CRF floor")
        self.assertEqual(achieved, 95.5)
        self.assertEqual(len(seen), len(set(seen)), "a CRF is probed at most once")
        self.assertEqual(sorted(p.crf for p in probes), sorted(seen))

    def test_a_source_below_even_the_relaxed_target_stays_unreachable(self):
        """The floor misses ceiling - margin too: the floor is reported, no extra probe."""
        chosen, achieved, _probes, effective, seen, measured = self._search(
            {30: 95.2, 31: 95.0, 32: 94.8, 33: 94.0, 34: 93.0, 35: 92.0},
            ceiling=98.46,
            margin=3.0,
        )
        self.assertAlmostEqual(effective, 95.46)
        measured.assert_called()
        self.assertEqual((chosen, achieved), (30, 95.2))
        self.assertLess(achieved, effective, "the caller's unreachable verdict must fire")
        self.assertEqual(seen, [33, 31, 30], "no probe is spent once the floor fails")

    def test_a_margin_never_raises_a_target_that_is_already_lower(self):
        chosen, _achieved, _probes, effective, _seen, _measured = self._search(
            {30: 89.2, 31: 89.0, 32: 88.0, 33: 87.0, 34: 86.0, 35: 85.0},
            ceiling=98.46,
            margin=3.0,
            target=90.0,
        )
        self.assertEqual(effective, 90.0)
        self.assertEqual(chosen, 30)

    def test_the_ceiling_is_the_mean_of_the_samples_against_themselves(self):
        calls: list[tuple[str, str]] = []

        def fake_measure(distorted, reference, *args, **kwargs):
            calls.append((Path(distorted).name, Path(reference).name))
            return 98.0

        with patch.object(engine_search, "measure_vmaf", side_effect=fake_measure):
            ceiling = engine_search.source_vmaf_ceiling(
                [(Path("a.mkv"), 0.0), (Path("b.mkv"), 10.0)],
                Path("work"),
            )
        self.assertEqual(ceiling, 98.0)
        self.assertEqual(calls, [("a.mkv", "a.mkv"), ("b.mkv", "b.mkv")])

    def test_an_empty_sample_list_cannot_measure_a_ceiling(self):
        with self.assertRaises(RuntimeError):
            engine_search.source_vmaf_ceiling([], Path("work"))


if __name__ == "__main__":
    unittest.main()
