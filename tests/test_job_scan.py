"""Tests for the encode job's payload scan (issue #374).

`execute_encode` used to resolve the payload's movies and pass the result
to `_encode_measured`, which ignored its `movies` argument and resolved the
payload again - two full library walks per encode job (the backlog payload
is the whole library root), the first thrown away. The job now scans once,
inside `_encode_measured`, and the one exclusion is the directory the job
writes into: the user's `output_dir` in two-directory mode, the per-run
staging directory in place.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db
from shrinkray.server import jobs


class TestEncodeJobSingleScan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        (self.root / "movie.mkv").write_bytes(b"x" * 16)
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _counting_resolve(self, calls: "list[Path | None]"):
        """Wraps the real resolve_movies behind a call recorder.

        The recorded value is the call's `exclude` argument; the return
        value is an empty list, so the job's encode loop stays out of the
        test - the pin is the scan wiring, not the per-file outcomes.
        """

        def wrapper(*args, **kwargs):
            calls.append(kwargs.get("exclude"))
            return []

        return wrapper

    def _run(self, payload: dict, calls: "list[Path | None]") -> dict:
        job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)
        with patch.object(jobs, "resolve_movies", side_effect=self._counting_resolve(calls)):
            return jobs.execute_encode(job, self.connection, threading.Event())

    def test_an_encode_job_resolves_its_payload_once(self) -> None:
        """One encode job, one payload scan (issue #374): the second walk the
        job used to run decided nothing and threw its result away.
        """
        out = self.root / "out"
        out.mkdir()
        calls: list[Path | None] = []
        result = self._run(
            {"paths": [str(self.root)], "quality": True, "output_dir": str(out)}, calls
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {"outcomes": [], "cancelled": False})

    def test_the_scan_excludes_the_output_directory_in_two_directory_mode(self) -> None:
        """The user's output_dir sits inside the scanned root and holds this
        job's (and later jobs') outputs: it must never re-enter the payload.
        """
        out = self.root / "out"
        out.mkdir()
        calls: list[Path | None] = []
        self._run({"paths": [str(self.root)], "quality": True, "output_dir": str(out)}, calls)
        self.assertEqual(calls, [out])

    def test_the_scan_excludes_the_staging_directory_in_place(self) -> None:
        """In place the job writes into a staging directory inside the
        scanned root; the scan must exclude exactly that directory.
        """
        calls: list[Path | None] = []
        self._run({"paths": [str(self.root)], "quality": True}, calls)
        self.assertEqual(len(calls), 1)
        exclude = calls[0]
        self.assertIsNotNone(exclude)
        assert exclude is not None
        self.assertEqual(exclude.parent, self.root)
        self.assertTrue(exclude.name.startswith(jobs.STAGING_DIR_PREFIX))


if __name__ == "__main__":
    unittest.main()
