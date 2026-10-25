"""plan's exit code mirrors the decisions it could not make (issue #203).

encode exits 1 when the engine cannot run (e.g. no HandBrakeCLI); plan used to
render the same failure as an error ROW and exit 0 - the error was a table
cell, not a failure. The contract: fail when NOTHING could be planned, stay at
0 when at least one movie got a verdict (an error row alongside results is a
partial answer, a skip is a successful decision).
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app
from shrinkray.engine.plan import QualityResult


@contextmanager
def _toolchain_present():
    """The plan preflight (#194) checks the toolchain before any work; these
    tests mock the engine itself, so the check must see a working one
    (CI runners have no ffprobe on PATH)."""
    with (
        patch("shrinkray.service.shutil.which", return_value="ffprobe"),
        patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
        patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
        patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
    ):
        yield


def _plan_result(movie: Path, *, success: bool, skipped: str | None = None) -> QualityResult:
    return QualityResult(
        input=movie,
        output=movie,
        success=success,
        skipped_reason=skipped,
        input_size=movie.stat().st_size,
        output_size=movie.stat().st_size,
    )


class TestPlanExitCode(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> int:
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            return runner.invoke(app, list(argv)).exit_code
        finally:
            os.chdir(previous)

    def test_a_plan_where_every_movie_errors_exits_nonzero(self) -> None:
        """No HandBrakeCLI: no verdict exists, so the command failed."""
        with (
            _toolchain_present(),
            patch(
                "shrinkray.engine.plan.plan_quality",
                side_effect=RuntimeError("'HandBrakeCLI' not found"),
            ),
        ):
            code = self._run("plan", str(self.movie))
        self.assertEqual(code, 1, "every movie failed to plan, yet plan exited 0")

    def test_a_plan_with_results_and_errors_still_exits_zero(self) -> None:
        """One unreadable file must not sink a library-wide plan."""
        second = self.root / "two.mkv"
        second.write_bytes(b"x" * 8)

        def fake_plan(movie, **kwargs):
            if movie.name == "one.mkv":
                raise RuntimeError("unreadable")
            return _plan_result(movie, success=True, skipped="too short")

        with (
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
        ):
            code = self._run("plan", str(self.root))
        self.assertEqual(code, 0, "a partial plan is still a plan")

    def test_a_plan_where_every_movie_is_skipped_exits_zero(self) -> None:
        """A skip is a successful decision, not a failure."""
        with (
            _toolchain_present(),
            patch(
                "shrinkray.engine.plan.plan_quality",
                side_effect=lambda m, **kwargs: _plan_result(m, success=False, skipped="too short"),
            ),
        ):
            code = self._run("plan", str(self.movie))
        self.assertEqual(code, 0, "every movie skipped, yet plan treated it as failure")


class TestPlanAppliesTheEligibilityGates(unittest.TestCase):
    """plan must hand the engine the same gates encode does (issue #246).

    It passed none of them, so the engine ran with its defaults: a 720p
    source was planned (and its plan recorded) that encode then skipped, and
    a raised floor or a size floor in config.json never reached plan.
    """

    def test_plan_forwards_the_configured_gates(self) -> None:
        from typer.testing import CliRunner

        from shrinkray import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / "one.mkv"
            movie.write_bytes(b"x" * 8)
            config_file = root / "config.json"
            config_file.write_text(
                '{"eligibility": {"min_source_height": 2160, "min_source_size_gb": 4.0,'
                ' "candidate_codecs": ["h264", "mpeg4"]}}',
                encoding="utf-8",
            )
            seen: dict = {}

            def fake_plan(m, **kwargs):
                seen.update(kwargs)
                return _plan_result(m, success=False, skipped="resolution too low")

            config_module.reset_config()
            try:
                with (
                    _toolchain_present(),
                    patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
                ):
                    result = CliRunner().invoke(
                        app, ["plan", str(movie), "--config", str(config_file)]
                    )
            finally:
                config_module.reset_config()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen.get("min_source_height"), 2160)
        self.assertEqual(seen.get("min_source_size_gb"), 4.0)
        self.assertEqual(seen.get("candidate_codecs"), ("h264", "mpeg4"))


class TestPlanUsesTheConfiguredVmaf(unittest.TestCase):
    """Issue #252: quality.target_vmaf is the default; --target-vmaf wins."""

    def _target_seen(self, *extra: str) -> float:
        from typer.testing import CliRunner

        from shrinkray import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "one.mkv"
            movie.write_bytes(b"x" * 8)
            config_file = Path(tmp) / "config.json"
            config_file.write_text('{"quality": {"target_vmaf": 94.0}}', encoding="utf-8")
            seen: dict = {}

            def fake_plan(m, **kwargs):
                seen.update(kwargs)
                return _plan_result(m, success=False, skipped="too short")

            config_module.reset_config()
            try:
                with (
                    _toolchain_present(),
                    patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
                ):
                    CliRunner().invoke(
                        app, ["plan", str(movie), "--config", str(config_file), *extra]
                    )
            finally:
                config_module.reset_config()
        return seen["target_vmaf"]

    def test_the_config_value_is_the_default(self) -> None:
        self.assertEqual(self._target_seen(), 94.0)

    def test_the_flag_wins(self) -> None:
        self.assertEqual(self._target_seen("--target-vmaf", "97"), 97.0)


if __name__ == "__main__":
    unittest.main()
