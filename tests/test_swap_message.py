"""Tests for the Replaced sentence's size (issue #430).

After a successful in-place swap the console sentence names the retained
original with a size in GB - and it named the NEW file's size (the outcome's
size_after), so on 29.05 GB -> 14.80 GB the operator read "the original is
retained ... (14.80 GB)" while the retained file held 29.05 GB. The sentence
exists to tell the operator what they keep; it must report size_before.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db, replace
from shrinkray.__main__ import _swap_into_place
from shrinkray.service import SwapOutcome


class TestReplacedSentenceSize(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL" * 100)  # 800 bytes
        self.output = self.root / "enc" / "movie.mkv"
        self.output.parent.mkdir()
        self.output.write_bytes(b"NEW" * 100)  # 300 bytes: smaller, swap passes
        self.connection = db.connect(self.root / "metadata.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_the_sentence_reports_the_originals_size(self) -> None:
        """The GB number in the Replaced sentence is the retained ORIGINAL's
        (size_before), not the new file's (size_before != size_after here).
        """
        with patch("shrinkray.__main__.console") as fake_console:
            _swap_into_place(self.connection, self.source, self.output)
        text = "".join(str(call.args[0]) for call in fake_console.print.call_args_list)
        # 800 bytes round to 0.00 GB either way; the faked-outcome test below
        # pins the number at the issue's magnitudes, this one pins that a real
        # swap's sentence prints at all and names the retained file
        self.assertIn("retained at", text)
        self.assertIn("movie.mkv" + replace.RETAINED_SUFFIX, text)

    def test_the_sentence_is_pinned_on_a_faked_outcome(self) -> None:
        """The issue's shape at readable magnitudes: a faked outcome with
        size_before=29.05 GB and size_after=14.80 GB must print 29.05.
        """
        outcome = SwapOutcome(
            replaced=True,
            retained_path=self.source.parent / (self.source.name + replace.RETAINED_SUFFIX),
            size_before=31_191_098_366,
            size_after=15_895_993_964,
        )
        with (
            patch("shrinkray.__main__.console") as fake_console,
            patch("shrinkray.service.swap_encode_into_place", return_value=outcome) as fake_swap,
        ):
            _swap_into_place(self.connection, self.source, self.output)
        fake_swap.assert_called_once()
        text = "".join(str(call.args[0]) for call in fake_console.print.call_args_list)
        self.assertIn("29.05 GB", text)
        self.assertNotIn("14.80 GB", text)


if __name__ == "__main__":
    unittest.main()
