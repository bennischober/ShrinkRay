import json
import tempfile
import unittest
from pathlib import Path

from shrinkray import db
from shrinkray.server.metrics import CONTENT_TYPE, render_metrics


class TestMetrics(unittest.TestCase):
    """Issue #168: the Prometheus exposition surface.

    ShrinkRay is an observable source, not its own sink. These tests pin the two
    properties a scraper depends on: the body parses as the text exposition
    format, and every value is a real query over existing state - so a wrong
    number is caught here rather than on someone's Grafana.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _render(self, root: Path | None = None) -> str:
        return render_metrics(self.connection, root)

    def _sample(self, body: str, name: str) -> str:
        for line in body.splitlines():
            if line.startswith(name + " ") or line.startswith(name + "{"):
                return line
        raise AssertionError(f"no sample for {name} in:\n{body}")

    def test_every_sample_has_a_help_and_type_line(self) -> None:
        body = self._render()
        for line in body.splitlines():
            if not line or line.startswith("#"):
                continue
            name = line.split("{")[0].split(" ")[0]
            self.assertIn(f"# HELP {name} ", body, f"{name} has no HELP line")
            self.assertIn(f"# TYPE {name} ", body, f"{name} has no TYPE line")

    def test_the_body_ends_with_a_newline(self) -> None:
        self.assertTrue(self._render().endswith("\n"))

    def test_an_empty_database_reports_zero_rather_than_omitting_the_metric(self) -> None:
        body = self._render()
        self.assertEqual(self._sample(body, "shrinkray_queue_depth"), "shrinkray_queue_depth 0")
        self.assertEqual(self._sample(body, "shrinkray_job_running"), "shrinkray_job_running 0")
        self.assertEqual(self._sample(body, "shrinkray_library_files"), "shrinkray_library_files 0")

    def test_job_state_counts_come_from_the_jobs_table(self) -> None:
        for state in ("queued", "queued", "running", "done", "failed"):
            self.connection.execute(
                "INSERT INTO jobs (kind, payload, state) VALUES ('encode', '{}', ?)", (state,)
            )
        self.connection.commit()
        body = self._render()
        self.assertEqual(self._sample(body, "shrinkray_queue_depth"), "shrinkray_queue_depth 2")
        self.assertEqual(self._sample(body, "shrinkray_job_running"), "shrinkray_job_running 1")
        self.assertIn('shrinkray_jobs{state="done"} 1', body)
        self.assertIn('shrinkray_jobs{state="cancelled"} 0', body)
        self.assertIn('shrinkray_jobs{state="interrupted"} 0', body)

    def _add_file(self, path: str = "/lib/a.mkv") -> int:
        cursor = self.connection.execute(
            "INSERT INTO files (path, mtime, size, name, category) VALUES (?, 1.0, 1, 'a', 'c')",
            (path,),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def test_reclaimed_gauge_row_sums_corroborated_rows(self) -> None:
        """The gauge row-sums the applied rows the records corroborate: a
        NULL-output_file row (no in-place claim) counts, so two such rows
        both count where the dashboard's latest-row total counts one. (A
        reachable in-place state can also corroborate an EARLIER row - a
        restore to an older generation's retained original followed by a
        re-encode at that generation's output size - so even the in-place
        row sum is not always the latest row alone.) What bytes reclaimed
        means across a chain is #646's question; this test pins the
        gauge's own rule."""
        file_id = self._add_file()
        for before, after in ((1000, 400), (500, 100)):
            self.connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size, saved_percent)"
                " VALUES (?, 'p', 'e', ?, ?, 0.0)",
                (file_id, before, after),
            )
        # the scan's record: the file sits at the LATEST generation's output
        self.connection.execute("UPDATE files SET size = 100 WHERE id = ?", (file_id,))
        self.connection.commit()
        body = self._render()
        self.assertEqual(self._sample(body, "shrinkray_encodes_total"), "shrinkray_encodes_total 2")
        self.assertEqual(
            self._sample(body, "shrinkray_bytes_reclaimed_total"),
            "shrinkray_bytes_reclaimed_total 1000",
        )

    def test_reclaimed_counts_an_output_dir_encode(self) -> None:
        """The read-only deployment (SHRINKRAY_WATCH_OUTPUT, docs/docker.md)
        never swaps: the source's recorded size stays at the input size, so
        the identity rule must not exclude an applied encode whose artifact
        lives at its own output path - the dashboard counts those rows, and
        the gauge must agree."""

        file_id = self._add_file()
        self.connection.execute(
            "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
            " saved_percent, outcome, output_file) VALUES"
            " (?, 'p', 'e', 1000, 400, 60.0, 'applied', '/output/a.mkv')",
            (file_id,),
        )
        self.connection.commit()
        body = self._render()
        self.assertEqual(
            self._sample(body, "shrinkray_bytes_reclaimed_total"),
            "shrinkray_bytes_reclaimed_total 600",
        )

    def test_reclaimed_excludes_a_missing_file(self) -> None:
        """A file the scans report missing keeps its last recorded size, but
        nothing is on disk - its record reclaims nothing (issue #647)."""

        file_id = self._add_file()
        self.connection.execute(
            "UPDATE files SET size = 400, missing_scans = 2 WHERE id = ?",
            (file_id,),
        )
        self.connection.execute(
            "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
            " saved_percent, outcome, output_file) VALUES"
            " (?, 'p', 'e', 1000, 400, 60.0, 'applied', '/lib/a.mkv')",
            (file_id,),
        )
        self.connection.commit()
        body = self._render()
        self.assertEqual(
            self._sample(body, "shrinkray_bytes_reclaimed_total"),
            "shrinkray_bytes_reclaimed_total 0",
        )

    def test_reclaimed_excludes_a_restored_original(self) -> None:
        """The identity rule (issue #647): the gauge counts only what the
        library's records corroborate. An applied IN-PLACE encode whose file
        no longer matches the recorded output size (the restore path: the
        retained original is back at the path) reclaims nothing - the record
        stays in the encode history and on the surfaces, not in the gauge's
        sum."""

        file_id = self._add_file()
        # the scan's record: the file is back to its ORIGINAL size
        self.connection.execute("UPDATE files SET size = 1000 WHERE id = ?", (file_id,))
        # an in-place encode: the row claims the file's own path
        self.connection.execute(
            "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
            " saved_percent, outcome, output_file) VALUES"
            " (?, 'p', 'e', 1000, 500, 50.0, 'applied', '/lib/a.mkv')",
            (file_id,),
        )
        self.connection.commit()
        body = self._render()
        self.assertEqual(
            self._sample(body, "shrinkray_bytes_reclaimed_total"),
            "shrinkray_bytes_reclaimed_total 0",
        )
        # the encoded state back on disk: the record counts again
        self.connection.execute("UPDATE files SET size = 500 WHERE id = ?", (file_id,))
        self.connection.commit()
        body = self._render()
        self.assertEqual(
            self._sample(body, "shrinkray_bytes_reclaimed_total"),
            "shrinkray_bytes_reclaimed_total 500",
        )

    def test_a_stalled_encode_is_counted_from_the_job_result(self) -> None:
        stalled = {
            "outcomes": [
                {
                    "movie": "a",
                    "state": "failed",
                    "error": "stalled: no HandBrake output for 10 min",
                },
                {"movie": "b", "state": "encoded", "error": None},
            ]
        }
        self.connection.execute(
            "INSERT INTO jobs (kind, payload, state, result) VALUES ('encode', '{}', 'done', ?)",
            (json.dumps(stalled),),
        )
        self.connection.commit()
        self.assertEqual(
            self._sample(self._render(), "shrinkray_encodes_stalled_total"),
            "shrinkray_encodes_stalled_total 1",
        )

    def test_retained_totals_come_from_the_ledger(self) -> None:
        self.connection.execute(
            "INSERT INTO retained (source_path, retained_path, size_before, size_after)"
            " VALUES ('a', 'a.orig', 800, 200)"
        )
        self.connection.execute(
            "INSERT INTO retained (source_path, retained_path, size_before, size_after)"
            " VALUES ('b', 'b.orig', 400, 100)"
        )
        self.connection.commit()
        body = self._render()
        self.assertEqual(
            self._sample(body, "shrinkray_retained_originals"), "shrinkray_retained_originals 2"
        )
        self.assertEqual(
            self._sample(body, "shrinkray_retained_bytes"), "shrinkray_retained_bytes 1200"
        )

    def test_the_disk_gauge_is_absent_without_a_root(self) -> None:
        body = self._render(None)
        self.assertNotIn("shrinkray_disk_free_bytes", body)

    def test_the_disk_gauge_reports_real_free_space(self) -> None:
        body = self._render(Path(self.tmp.name))
        line = self._sample(body, "shrinkray_disk_free_bytes")
        self.assertGreater(int(line.rsplit(" ", 1)[1]), 0)

    def test_an_unreadable_root_is_not_reported_as_a_full_disk(self) -> None:
        body = self._render(Path(self.tmp.name) / "does-not-exist" / "deeper")
        self.assertNotIn("shrinkray_disk_free_bytes 0", body)

    def test_the_content_type_is_the_prometheus_text_format(self) -> None:
        self.assertEqual(CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")
