"""A declined swap must not be reported as an encode (issue #625).

`record_quality_results` writes the full encode row when the encode measured at
target; the swap then declines (a lost track, a changed source, not smaller, a
`SwapError`) and the artifact is discarded - but the row keeps its preset, sizes
and saved_percent, so every surface reports the file as ENCODED with a
reclaimed-size figure. Hard invariant 5 (honest about failure) broken at the
data layer.

The fix under test: `encodes.outcome` ('applied' | 'discarded' | 'skipped') with
`outcome_reason`; the snapshot reports the attempt with its outcome, the metrics
exclude non-applied rows.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray import db
from shrinkray.service import swap_encode_into_place


class TestDeclinedSwapIsNotAnEncode(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")
        self.source = Path(self.tmp.name) / "source.mkv"
        self.source.write_bytes(b"x" * 1000)
        self.output = Path(self.tmp.name) / "staged.mkv"
        self.output.write_bytes(b"x" * 400)
        self.file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=self.source,
                mtime=self.source.stat().st_mtime,
                size=1000,
                name="source",
                category="Cat",
            ),
        )
        # the row record_quality_results writes on success: the full measured
        # row, its output_file pointing at the staging artifact
        self.encode_id = db.add_encode(
            self.connection,
            self.file_id,
            "VMAF>96",
            "svt_av1",
            1000,
            400,
            60.0,
            chosen_crf=22,
            achieved_vmaf=96.03,
            output_file=str(self.output),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _decline(self, error: str):
        return swap_encode_into_place(
            self.connection,
            self.source,
            self.output,
            encode_id=self.encode_id,
            error=error,
        )

    def _snapshot_library_encode(self):
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            body = client.get("/api/snapshot").json()
            movie = next(m for m in body["library"] if m["name"] == "source")
            return movie["encode"]
        finally:
            # an unclosed AppState leaks its sqlite connection, which pytest
            # reports as an unraisable ResourceWarning at the NEXT test that
            # collects unraisables - not this one
            app_module.state.shutdown()
            app_module.state = None

    def test_a_declined_swap_is_not_reported_as_encoded(self) -> None:
        """The issue's reproduction: ENCODED · 62.9% · RECLAIMED for a file
        that is still the original bytes on disk."""
        outcome = self._decline("output lost 2 subtitle track(s)")
        self.assertFalse(outcome.replaced)
        encode = self._snapshot_library_encode()
        self.assertIsNotNone(encode, "the attempt must stay visible, as an attempt")
        self.assertEqual(
            encode["outcome"],
            "discarded",
            "a declined swap must not read as an applied encode",
        )
        self.assertIn("subtitle track", encode["outcome_reason"])

    def test_every_decline_path_records_the_outcome(self) -> None:
        """Verification failure, changed source, not smaller - each declines,
        each records the outcome with its reason."""
        for error in (
            "output lost 2 subtitle track(s)",
            "the source changed while encoding",
        ):
            with self.subTest(error=error):
                outcome = self._decline(error)
                self.assertFalse(outcome.replaced)
                encode = self._snapshot_library_encode()
                self.assertEqual(encode["outcome"], "discarded")
                self.assertTrue(encode["outcome_reason"])

    def test_the_not_smaller_path_records_the_outcome(self) -> None:
        """The size gate declines without an error string: a real file pair
        whose output is not smaller."""
        bigger = Path(self.tmp.name) / "bigger.mkv"
        bigger.write_bytes(b"x" * 2000)
        outcome = swap_encode_into_place(
            self.connection, self.source, bigger, encode_id=self.encode_id
        )
        self.assertFalse(outcome.replaced)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("not smaller", encode["outcome_reason"])

    def test_the_changed_source_path_records_the_outcome(self) -> None:
        """The identity gate (#585): a source that changed during the encode
        declines, and the row records the decline."""
        outcome = swap_encode_into_place(
            self.connection,
            self.source,
            self.output,
            encode_id=self.encode_id,
            input_size=999,
            input_mtime=12345.0,
        )
        self.assertFalse(outcome.replaced)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("changed", encode["outcome_reason"])

    def test_the_swap_error_path_records_the_outcome(self) -> None:
        """A refused rename (SwapError) declines, and the row records it."""
        from unittest.mock import patch

        from shrinkray.replace import SwapError

        with patch(
            "shrinkray.service.swap_into_library",
            side_effect=SwapError("the rename was refused"),
        ):
            outcome = swap_encode_into_place(
                self.connection, self.source, self.output, encode_id=self.encode_id
            )
        self.assertFalse(outcome.replaced)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("refused", encode["outcome_reason"])

    def test_the_watchers_declines_record_the_outcome(self) -> None:
        """The watcher's own swap implementation (engine/watch.py::_swap) has
        the same four declines (issue #625 round 1): the resident path must
        record the outcome too, not only the service's swap."""
        from shrinkray.engine.watch import _swap

        outcome = _swap(
            Path(self.tmp.name) / "test.db",
            self.source,
            self.output,
            encode_id=self.encode_id,
            error="output lost 2 subtitle track(s)",
        )
        self.assertFalse(outcome)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("subtitle track", encode["outcome_reason"])

        outcome = _swap(
            Path(self.tmp.name) / "test.db",
            self.source,
            self.output,
            encode_id=self.encode_id,
            input_size=999,
            input_mtime=12345.0,
        )
        self.assertFalse(outcome)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("changed", encode["outcome_reason"])

        with patch_shrinkray_replace_swap():
            outcome = _swap(
                Path(self.tmp.name) / "test.db",
                self.source,
                self.output,
                encode_id=self.encode_id,
            )
        self.assertFalse(outcome)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertTrue(encode["outcome_reason"])

    def test_metrics_exclude_discarded_encodes(self) -> None:
        """The encode counter and the reclaimed-bytes gauge must not count a
        discarded attempt (the dashboard's RECLAIMED 16.0 GB came from here)."""
        from shrinkray.server.metrics import render_metrics

        self._decline("output lost 2 subtitle track(s)")
        text = render_metrics(self.connection, None)
        self.assertIn("shrinkray_encodes_total 0", text)
        self.assertIn("shrinkray_bytes_reclaimed_total 0", text)
        self.assertIn("shrinkray_encodes_discarded_total 1", text)

    def test_an_applied_encode_still_reports_encoded(self) -> None:
        """The guard: a healthy encode's row keeps reporting as applied."""
        encode = self._snapshot_library_encode()
        self.assertIsNotNone(encode)
        self.assertEqual(encode["outcome"], "applied")
        self.assertIsNone(encode["outcome_reason"])

    def test_the_watchers_not_smaller_decline_records_the_outcome(self) -> None:
        """The watcher's size gate is its fourth decline (issue #625 round 2):
        the earlier pins never drove it because their output was smaller."""
        from shrinkray.engine.watch import _swap

        bigger = Path(self.tmp.name) / "bigger.mkv"
        bigger.write_bytes(b"x" * 2000)
        outcome = _swap(
            Path(self.tmp.name) / "test.db",
            self.source,
            bigger,
            encode_id=self.encode_id,
        )
        self.assertFalse(outcome)
        encode = self._snapshot_library_encode()
        self.assertEqual(encode["outcome"], "discarded")
        self.assertIn("not smaller", encode["outcome_reason"])

    def test_the_library_etag_moves_on_a_decline(self) -> None:
        """The ETag fix (issue #625 round 1): an outcome update must change the
        fingerprint, or the client's If-None-Match keeps the stale library."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            first = client.get("/api/snapshot")
            etag = first.json()["library_etag"]
            self._decline("output lost 2 subtitle track(s)")
            stale = client.get("/api/snapshot", headers={"If-None-Match": etag})
            self.assertNotEqual(
                stale.json().get("library"),
                None,
                "the ETag must move on an outcome update, or the client keeps ENCODED",
            )
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_the_library_route_carries_the_outcome(self) -> None:
        """/api/library is a second copy of the snapshot's query (issue #625
        round 1): its encode block carries the same fields."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        self._decline("output lost 2 subtitle track(s)")
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            body = client.get("/api/library").json()
            movie = next(m for m in body if m["name"] == "source")
            self.assertEqual(movie["encode"]["outcome"], "discarded")
            self.assertIn("subtitle track", movie["encode"]["outcome_reason"])
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_the_movie_history_carries_the_outcome(self) -> None:
        """/api/movie's history lists the discarded attempt with its reason
        (the issue's DoD)."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        self._decline("output lost 2 subtitle track(s)")
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            app_module.allow_roots([Path(self.tmp.name)])
            client = TestClient(fast_app)
            body = client.get("/api/movie", params={"path": str(self.source)}).json()
            entry = next(h for h in body["history"] if h["id"] == self.encode_id)
            self.assertEqual(entry["outcome"], "discarded")
            self.assertIn("subtitle track", entry["outcome_reason"])
        finally:
            app_module.state.shutdown()
            app_module.state = None


def patch_shrinkray_replace_swap():
    """replace.swap is imported INSIDE _swap (`from shrinkray import db, replace`),
    so the patch seam is the replace module's own attribute."""
    from unittest.mock import patch

    from shrinkray.replace import SwapError

    return patch("shrinkray.replace.swap", side_effect=SwapError("the rename was refused"))


if __name__ == "__main__":
    unittest.main()
