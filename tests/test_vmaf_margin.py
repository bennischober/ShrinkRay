"""The effective VMAF target: an absolute floor or a margin below the ceiling.

The VMAF model does not score identical frames as 100: its maximum is
content-dependent (measured 97.4-99.9 across four shapes, issue #584), so an
absolute target is stricter on flat content than on a detailed film and
unreachable where the ceiling sits below it. `quality.vmaf_margin` turns the
target into `min(target_vmaf, ceiling - vmaf_margin)`; 0 keeps the absolute
target exactly (issue #584, the maintainer's decision).
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
        self.assertEqual(effective, 96.0, "margin 0 must keep the absolute target")
        self.assertEqual(chosen, 30)
        measured.assert_not_called()

    def test_a_margin_clamps_the_target_below_the_ceiling(self):
        """A ceiling of 98.46 with margin 3 aims at 95.46, not 96."""
        chosen, _achieved, _probes, effective, _seen, measured = self._search(
            {30: 96.2, 31: 95.6, 32: 95.0, 33: 94.0, 34: 93.0, 35: 92.0},
            ceiling=98.46,
            margin=3.0,
        )
        self.assertAlmostEqual(effective, 95.46)
        # 31 passes the clamped target (95.6 >= 95.46) and 32 does not
        self.assertEqual(chosen, 31)
        measured.assert_called()

    def test_a_margin_never_raises_a_target_that_is_already_lower(self):
        """The clamp is a min: a target below ceiling - margin is untouched."""
        chosen, _achieved, _probes, effective, _seen, _measured = self._search(
            {30: 90.2, 31: 89.0, 32: 88.0, 33: 87.0, 34: 86.0, 35: 85.0},
            ceiling=98.46,
            margin=3.0,
            target=90.0,
        )
        self.assertEqual(effective, 90.0, "the margin must never lower a lower target")
        self.assertEqual(chosen, 30)

    def test_the_ceiling_is_the_mean_of_the_samples_against_themselves(self):
        """One VMAF pass per sample, each against itself."""
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
