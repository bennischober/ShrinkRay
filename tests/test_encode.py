"""Encode engine tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from shrinkray import config as config_helper
from shrinkray import db
from shrinkray.engine import encoders as engine_encoders
from shrinkray.engine import pipeline as encode
from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.tools import ffprobe as tools_ffprobe
from shrinkray.tools import handbrake as tools_handbrake
from shrinkray.tools import process as tools_process
from shrinkray.tools import vmaf as tools_vmaf


class TestEncode(unittest.TestCase):
    def test_tracks_survived_reports_a_lost_track(self):
        """Issue #240's verification: an output with fewer audio/subtitle
        streams than the source is reported, not silently accepted."""
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")

            def fake_counts(file: Path):
                if file == src:
                    return {("audio", "deu"): 2, ("subtitle", "eng"): 1}
                return {("audio", "deu"): 1}

            with mock.patch("shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts):
                ok, detail = tracks_survived(src, out)
        self.assertFalse(ok)
        self.assertIn("1 audio track(s)", detail)
        self.assertIn("1 subtitle track(s)", detail)

    def test_tracks_survived_passes_when_everything_survived(self):
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")

            def fake_counts(file: Path):
                return {("audio", "deu"): 2, ("subtitle", "eng"): 1}

            with mock.patch("shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts):
                ok, detail = tracks_survived(src, out)
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_tracks_survived_honours_the_language_filters(self):
        """Review finding 2 (mechanical half): a track the configured filter
        removes on purpose is expected missing - only a track the filter
        should have kept can fail the check."""
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")

            # source: deu + fra audio, eng subtitles; output: only deu audio
            def fake_counts(file: Path):
                if file == src:
                    return {("audio", "deu"): 1, ("audio", "fra"): 1, ("subtitle", "eng"): 1}
                return {("audio", "deu"): 1}

            with mock.patch("shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts):
                # the defaults: fra audio is outside the filter (expected
                # missing, NOT a failure) and eng subtitles are inside it
                ok, detail = tracks_survived(
                    src,
                    out,
                    audio_languages=["deu", "eng", "und", "jpn"],
                    subtitle_languages=["eng", "deu", "und"],
                )
                self.assertFalse(ok)
                self.assertNotIn("audio", detail)
                self.assertIn("1 subtitle track(s)", detail)

                # the eng subtitle SURVIVES: only the fra loss remains, and
                # the filter covers it => the check passes
                def fake_counts_ok(file: Path):
                    if file == src:
                        return {("audio", "deu"): 1, ("audio", "fra"): 1, ("subtitle", "eng"): 1}
                    return {("audio", "deu"): 1, ("subtitle", "eng"): 1}

                with mock.patch(
                    "shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts_ok
                ):
                    ok, detail = tracks_survived(
                        src,
                        out,
                        audio_languages=["deu", "eng", "und", "jpn"],
                        subtitle_languages=["eng", "deu", "und"],
                    )
                    self.assertTrue(ok)
                    self.assertEqual(detail, "")
                # no filter: both losses are failures (the 1:1 default)
                ok, detail = tracks_survived(src, out)
                self.assertFalse(ok)
                self.assertIn("1 audio track(s)", detail)
                self.assertIn("1 subtitle track(s)", detail)

    def test_tracks_survived_knows_every_639_2_b_t_pair(self):
        """Review of #287: only ger/deu and fre/fra were paired, so a filter of
        `chi` with a `zho`-tagged output reported a lost track on a good
        encode. Every B/T pair is the same language."""
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")
            for b_code, t_code in (("chi", "zho"), ("cze", "ces"), ("dut", "nld"), ("wel", "cym")):

                def fake_counts(file: Path, b=b_code, t=t_code):
                    return {("audio", b): 1} if file == src else {("audio", t): 1}

                with mock.patch(
                    "shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts
                ):
                    ok, detail = tracks_survived(src, out, audio_languages=[b_code])
                self.assertTrue(ok, f"{b_code}/{t_code}: {detail}")

    def test_tracks_survived_matches_both_639_2_spellings(self):
        """HandBrake accepts B and T codes; the muxer tags the output with
        whichever spelling the container carried. deu/ger and fra/fre are
        the same language for the 1:1 check."""
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")

            def fake_counts(file: Path):
                if file == src:
                    return {("audio", "deu"): 1, ("audio", "fra"): 1}
                # the muxer wrote B-code tags
                return {("audio", "ger"): 1, ("audio", "fre"): 1}

            with mock.patch("shrinkray.engine.pipeline._stream_counts", side_effect=fake_counts):
                ok, detail = tracks_survived(src, out, audio_languages=["deu", "fre"])
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_tracks_survived_reports_unverifiable_as_failure(self):
        """Review finding 4: an ffprobe failure is UNMEASURED, not passed -
        the check fails with unverifiable, and the swap gate treats an
        unverified artifact like a broken one."""
        from shrinkray.engine.pipeline import tracks_survived

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x")
            out.write_bytes(b"x")
            with mock.patch(
                "shrinkray.engine.pipeline._stream_counts",
                side_effect=RuntimeError("ffprobe not found"),
            ):
                ok, detail = tracks_survived(src, out)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("unverifiable:"))
        self.assertNotIn("1 audio track", detail)

    def test_the_encode_command_carries_the_240_contract(self):
        """Issue #240: the container is pinned to MKV, the copy mask covers
        the passthru vocabulary, the fallback is lossless, chapters survive,
        and the language filters ride WITH the all-track flags."""
        from shrinkray.tools.handbrake import build_encode_command

        command = build_encode_command(
            "hb",
            Path("in.mkv"),
            Path("out.mkv"),
            20,
            "svt_av1",
            "3",
            audio_languages=["deu", "eng", "und", "jpn"],
            subtitle_languages=["eng", "deu", "und"],
        )
        joined = command
        # the muxer is pinned: a non-MKV source cannot silently drop
        # PGS/DVB subtitles into an MP4
        self.assertIn("-f", joined)
        self.assertEqual(joined[joined.index("-f") + 1], "av_mkv")
        # the passthru mask: the 12 names that exist in 1.6-1.11
        mask = joined[joined.index("--audio-copy-mask") + 1]
        for name in (
            "aac",
            "ac3",
            "dts",
            "dtshd",
            "eac3",
            "flac",
            "alac",
            "mp2",
            "mp3",
            "opus",
            "truehd",
            "vorbis",
        ):
            self.assertIn(f"copy:{name}", mask)
        self.assertNotIn("copy:pcm", mask, "copy:pcm fails the job on 1.6-1.9")
        # the fallback is lossless
        self.assertEqual(joined[joined.index("--audio-fallback") + 1], "flac24")
        # chapters survive
        self.assertIn("--markers", joined)
        # the filter flags travel WITH the all-track flags (the list alone
        # sets selection to first and would drop every track after it)
        self.assertLess(joined.index("--all-audio"), joined.index("--audio-lang-list"))
        self.assertLess(joined.index("--all-subtitles"), joined.index("--subtitle-lang-list"))
        self.assertEqual(joined[joined.index("--audio-lang-list") + 1], "deu,eng,und,jpn")
        self.assertEqual(joined[joined.index("--subtitle-lang-list") + 1], "eng,deu,und")

    def test_the_language_filters_default_to_every_language(self):
        """None/empty lists = no filter flags at all: every track, every
        language (the old behaviour, and the documented opt-out)."""
        from shrinkray.tools.handbrake import build_encode_command

        command = build_encode_command("hb", Path("in.mkv"), Path("out.mkv"), 20, "svt_av1", "3")
        self.assertNotIn("--audio-lang-list", command)
        self.assertNotIn("--subtitle-lang-list", command)

    def test_the_container_follows_the_source_extension(self):
        """Review finding 3: the muxer is pinned (av_mkv) for an .mkv source
        only - a non-MKV source keeps the extension-derived muxer so its
        bytes and name agree; the 1:1 check catches what that muxer drops."""
        from shrinkray.tools.handbrake import build_encode_command

        mkv = build_encode_command("hb", Path("in.mkv"), Path("out.mkv"), 20, "svt_av1", "3")
        self.assertIn("-f", mkv)
        self.assertEqual(mkv[mkv.index("-f") + 1], "av_mkv")

        mp4 = build_encode_command("hb", Path("in.mp4"), Path("out.mp4"), 20, "svt_av1", "3")
        self.assertNotIn("-f", mp4)

        # uppercase suffix: same rule
        m4v = build_encode_command("hb", Path("in.MKV"), Path("out.MKV"), 20, "svt_av1", "3")
        self.assertIn("-f", m4v)

    def test_cap_threads(self):
        """The thread cap normalizes 0/None/negative to uncapped."""
        self.assertIsNone(engine_pipeline.cap_threads(None))
        self.assertIsNone(engine_pipeline.cap_threads(0))
        self.assertIsNone(engine_pipeline.cap_threads(-3))
        self.assertEqual(engine_pipeline.cap_threads(8), 8)

    def test_encoder_for_is_the_single_source_of_the_codec(self):
        """encoder_for is the only place the output codec is chosen.

        PRODUCT.md makes AV1 the single output codec (issue #21): SDR uses
        svt_av1 and HDR svt_av1_10bit. The six HDR ternaries that used to
        be written per call site had already drifted once (#26), so the choice
        lives in one function and every caller reads it from there. Since
        issue #234 the source's bit depth joins the decision.
        """
        self.assertEqual(engine_encoders.encoder_for(8, False), "svt_av1")
        self.assertEqual(engine_encoders.encoder_for(10, True), "svt_av1_10bit")

    def test_no_handbrake_command_emits_h264_or_h265(self):
        """No code path emits H.264 or H.265 any more (issue #21).

        x265 is gone from every path - not demoted to a fallback, not kept
        as an option. The check is on the HandBrake command only: the
        ffmpeg sample extraction uses libx264 for intermediate clips, which
        is not an output codec.
        """
        for command in (
            tools_handbrake.build_encode_command(
                "hb",
                Path("in.mkv"),
                Path("out.mkv"),
                30,
                engine_encoders.encoder_for(8, False),
                "3",
            ),
            tools_handbrake.build_encode_command(
                "hb",
                Path("in.mkv"),
                Path("out.mkv"),
                30,
                engine_encoders.encoder_for(10, True),
                "3",
            ),
        ):
            joined = " ".join(command)
            for gone in ("x265", "x264", "x265_10bit", "h265", "h264"):
                self.assertNotIn(gone, joined)
            self.assertIn("svt_av1", joined)

    def test_saved_percent(self):
        result = encode.EncodeResult(
            input=Path("in.mkv"),
            output=Path("out.mkv"),
            success=True,
            input_size=1000,
            output_size=400,
        )
        self.assertEqual(result.saved_percent, 60.0)
        # larger output => negative
        result = encode.EncodeResult(
            input=Path("in.mkv"),
            output=Path("out.mkv"),
            success=True,
            input_size=1000,
            output_size=1500,
        )
        self.assertEqual(result.saved_percent, -50.0)
        # zero sizes => 0
        result = encode.EncodeResult(input=Path("in.mkv"), output=Path("out.mkv"), success=True)
        self.assertEqual(result.saved_percent, 0.0)

    def test_streamed_encode_reaps_exit_code(self):
        """The streamed encode (progress path) must reap the process: at EOF
        Popen.returncode is still None, and reading it without wait() marked
        every successful streamed encode as failed with the whole HandBrake
        log as its error (job #20 was the first full streamed encode and
        exposed this)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x" * 8)
            # cmd exits 0 and closes its pipes: EOF on stdout is reached,
            # so the streamed loop ends without cancellation
            command = ["cmd", "/c", "exit 0"] if os.name == "nt" else ["true"]
            result = engine_pipeline._encode_with_command(
                src, out, command, progress=lambda p: None
            )
            # the fake encode wrote no output file: the command itself must
            # still have been reaped as SUCCESS (exit 0), reaching the
            # "no output file" check rather than "handbrake failed"
            self.assertIn(result.error in (None, "no output file was written"), (True,))
            self.assertTrue(
                result.success or result.error == "no output file was written",
                f"streamed run misread exit code: success={result.success} error={result.error!r}",
            )

    def test_cancel_responds_during_silence(self):
        """The cancel event must interrupt an encode whose child has gone
        SILENT: the old readline() loop only checked cancel between lines,
        so a hung HandBrake was uninterruptible even by an explicit user
        cancel. The queue-based loop polls every 0.2s."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            # a child that emits one line, then goes silent forever
            if os.name == "nt":
                stub = Path(tmp) / "hb_stub.cmd"
                stub.write_bytes(
                    b"@echo off\r\necho Encoding: task 1 of 1, 42.31 %%\r\n"
                    b"ping -n 60 127.0.0.1 >nul\r\n"
                )
            else:
                stub = Path(tmp) / "hb_stub.sh"
                stub.write_bytes(b'#!/bin/sh\necho "Encoding: task 1 of 1, 42.31 %"\nsleep 60\n')
                stub.chmod(0o755)
            cancel = threading.Event()
            result_holder: list = []

            def runner():
                result_holder.append(
                    engine_pipeline._encode_with_command(
                        src, out, [str(stub)], progress=lambda p: None, cancel=cancel
                    )
                )

            thread = threading.Thread(target=runner)
            thread.start()
            time.sleep(2.0)  # let the child print its one line and go silent
            self.assertEqual(len(result_holder), 0, "encode returned before cancel")
            cancel.set()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "cancel was not honored during silence")
            result = result_holder[0]
            self.assertTrue(result.cancelled)
            self.assertEqual(result.error, "cancelled by user")
            self.assertFalse(out.exists())

    def test_cr_progress_never_looks_stalled(self):
        """THE round-6.11 regression: a child streaming \\r-separated
        progress (real HandBrake's piped shape — measured: progress lines
        contain NO \\n, largest real gap 0.27s) must NEVER be killed by
        the stall watchdog. The old readline() pump never saw those bytes,
        so a healthy encode died after 10 min ('stalled') with its cores
        fully busy. Percent values split across chunks must still parse."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            # a child that streams \r-only progress CONTINUOUSLY (gaps of
            # 0.2s, far inside the watchdog window) for ~6s and then
            # exits: with the chunk pump the watchdog can never fire while
            # updates flow — the old readline() pump saw NOTHING and would
            # have killed this child at the 5s mark
            stub = Path(tmp) / "hb_stub.py"
            stub.write_text(
                "import sys, time\n"
                "for i in range(1, 31):\n"
                "    sys.stdout.write(f'Encoding: task 1 of 1, {i}.00 %')\n"
                "    sys.stdout.write('\\r')\n"
                "    sys.stdout.flush()\n"
                "    time.sleep(0.2)\n",
                encoding="utf-8",
            )
            percents: list[float] = []
            with (
                mock.patch("shrinkray.engine.pipeline.stall_timeout_seconds", return_value=5.0),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
            ):
                result = engine_pipeline._encode_with_command(
                    src,
                    out,
                    [sys.executable, str(stub)],
                    progress=lambda d: percents.append(d["percent"]),
                )
            # ~40 \r-updates over ~12-40s of child runtime: the watchdog
            # (2s) must NEVER have fired despite updates being \r-only...
            self.assertFalse(
                (result.error or "").startswith("stalled"),
                f"watchdog killed a \\r-streaming child: {result.error!r}",
            )
            # ...and the percent parser was fed by the \r lines
            self.assertGreaterEqual(len(percents), 10, f"percents: {percents[:5]}...")
            self.assertIn(1.0, percents)
            self.assertIn(30.0, percents)

    def test_percent_split_across_chunks_still_parses(self):
        """A percent value split mid-number across raw writes ('12.3' | '4 %')
        must still produce one percent event (the tail buffer)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            # printf byte-exact pieces: 'Encoding: task 1 of 1, 12.3' then '4 %'
            stub = Path(tmp) / "hb_stub_split.py"
            stub.write_text(
                "import sys, time\n"
                "sys.stdout.write('Encoding: task 1 of 1, 12.3')\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.5)\n"
                "sys.stdout.write('4 %')\n"
                "sys.stdout.flush()\n",
                encoding="utf-8",
            )
            percents: list[float] = []
            with mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True):
                engine_pipeline._encode_with_command(
                    src,
                    out,
                    [sys.executable, str(stub)],
                    progress=lambda d: percents.append(d["percent"]),
                )
            self.assertIn(12.34, percents, f"percents: {percents}")

    def test_stall_watchdog_terminates_hung_child(self):
        """No child output for stall_minutes => the engine terminates the
        hung child itself, removes the partial and reports a FAILURE
        (cancelled=False) so callers do not retry a stalling encode."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            if os.name == "nt":
                stub = Path(tmp) / "hb_stub.cmd"
                stub.write_bytes(
                    b"@echo off\r\necho Encoding: task 1 of 1, 42.31 %%\r\n"
                    b"ping -n 60 127.0.0.1 >nul\r\n"
                )
            else:
                stub = Path(tmp) / "hb_stub.sh"
                stub.write_bytes(b'#!/bin/sh\necho "Encoding: task 1 of 1, 42.31 %"\nsleep 60\n')
                stub.chmod(0o755)
            with mock.patch("shrinkray.engine.pipeline.stall_timeout_seconds", return_value=1.0):
                result = engine_pipeline._encode_with_command(
                    src, out, [str(stub)], progress=lambda p: None
                )
            self.assertFalse(result.success)
            self.assertFalse(result.cancelled, "a stall must be a failure, not a cancel")
            self.assertIn("stall", (result.error or "").lower())
            self.assertFalse(out.exists())

    def test_stall_disabled_never_terminates(self):
        """stall_minutes=0 disables the watchdog: a silent child runs until
        it finishes or the user cancels."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            with mock.patch("shrinkray.engine.pipeline.stall_timeout_seconds", return_value=None):
                # a silent-but-finishing child: no percent lines at all
                command = ["cmd", "/c", "exit 0"] if os.name == "nt" else ["true"]
                result = engine_pipeline._encode_with_command(
                    src, out, command, progress=lambda p: None
                )
            self.assertFalse(result.cancelled)

    def test_configure_stall_knob(self):
        """The config knob maps minutes to seconds; 0/negative disables."""

        old = tools_process.stall_timeout_seconds()
        try:
            tools_process.configure_stall(2.5)
            self.assertEqual(tools_process.stall_timeout_seconds(), 150.0)
            tools_process.configure_stall(0)
            self.assertIsNone(tools_process.stall_timeout_seconds())
            tools_process.configure_stall(-1)
            self.assertIsNone(tools_process.stall_timeout_seconds())
        finally:
            tools_process.configure_stall((old or 600.0) / 60.0)

    def test_quality_probe_run_stall_is_failure_not_cancel(self):
        """_run reports a stall as code -2 with a stall message - the probe
        callers map it to RuntimeError (failure), never Cancelled."""
        from shrinkray.tools.process import _run

        # a SILENT child (ping's own replies would count as progress)
        command = ["cmd", "/c", "ping -n 60 127.0.0.1 >nul"] if os.name == "nt" else ["sleep", "60"]
        # _run lives in tools.process now (phase 2): the stall lookup happens
        # in ITS namespace, so patch there (patch where the name is looked up)
        with mock.patch("shrinkray.tools.process.stall_timeout_seconds", return_value=1.0):
            code, err = _run(command)
        self.assertEqual(code, -2)
        self.assertIn("stalled", err)

    def test_run_timeout_terminates_the_child_without_a_cancel_event(self):
        """Issue #384: a bounded read (the crop pass's duration ceiling)
        passes timeout= and no cancel event; with the stall watchdog off,
        the runner must still take the watched path and terminate the child
        at the ceiling, reporting -2 like a stall."""
        from shrinkray.tools.process import _run

        command = ["cmd", "/c", "ping -n 60 127.0.0.1 >nul"] if os.name == "nt" else ["sleep", "60"]
        with mock.patch("shrinkray.tools.process.stall_timeout_seconds", return_value=None):
            started = time.monotonic()
            code, err = _run(command, timeout=0.5)
            elapsed = time.monotonic() - started
        self.assertEqual(code, -2)
        self.assertIn("timed out", err)
        self.assertLess(elapsed, 10.0, "the child ran past its ceiling")

    def test_terminated_tree_does_not_hang_on_orphaned_grandchild(self):
        """Regression (the 59-second hang): killing the direct child must
        not leave the caller waiting for an orphaned grandchild that holds
        the pipe write-ends. taskkill /T /F (Windows) kills the whole tree;
        the drain threads reach EOF immediately."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            # parent (cmd) spawns a LONG-running grandchild (ping) that
            # inherits the pipes and would keep them open for ~58s
            if os.name == "nt":
                stub = Path(tmp) / "hb_stub.cmd"
                stub.write_bytes(b"@echo off\r\nping -n 60 127.0.0.1 >nul\r\n")
            else:
                stub = Path(tmp) / "hb_stub.sh"
                stub.write_bytes(b'#!/bin/sh\nsh -c "sleep 60" & sleep 60\n')
                stub.chmod(0o755)
            with mock.patch("shrinkray.engine.pipeline.stall_timeout_seconds", return_value=1.0):
                started = time.monotonic()
                result = engine_pipeline._encode_with_command(
                    src, out, [str(stub)], progress=lambda p: None
                )
                elapsed = time.monotonic() - started
            # the stall watchdog fired at ~1s and the FUNCTION RETURNED
            # promptly (old code: 58s waiting for the orphan)
            self.assertFalse(result.success)
            self.assertFalse(result.cancelled)
            self.assertLess(elapsed, 15.0, f"orphan hang: returned after {elapsed:.1f}s")
            self.assertFalse(out.exists())

    def test_stall_writes_telemetry_bundle(self):
        """A stall kill with db_file leaves the evidence bundle in logs/
        (regression: the stall path returned silently - logs/ never even
        existed after one - and the child's collected stderr was
        overwritten by the one-line stall message)."""
        import tempfile

        from shrinkray.telemetry import LOGS_DIRNAME

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "enc" / "out.mkv"
            (Path(tmp) / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            db_path = Path(tmp) / "test.db"
            db_path.write_bytes(b"")  # logs/ is created next to it
            stub = Path(tmp) / "hb_stub.cmd"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\nping -n 60 127.0.0.1 >nul\r\n")
            else:
                stub.write_bytes(b"#!/bin/sh\nsleep 60\n")
                stub.chmod(0o755)
            with mock.patch("shrinkray.engine.pipeline.stall_timeout_seconds", return_value=1.0):
                result = engine_pipeline._encode_with_command(
                    src, out, [str(stub)], progress=lambda p: None, db_file=db_path
                )
            self.assertFalse(result.success)
            self.assertFalse(result.cancelled)
            bundles = list((db_path.parent / LOGS_DIRNAME).iterdir())
            self.assertEqual(len(bundles), 1, f"expected one stall bundle: {bundles}")
            # the bundle carries the stall reason AND the child's stderr
            text = (bundles[0] / "stderr.txt").read_text(encoding="utf-8", errors="replace")
            self.assertIn("stalled", text)

    def test_disk_watchdog_cancels_when_space_drops(self):
        """When free space on the output drive drops below the floor, the
        streamed encode is cancelled: the process is terminated, the partial
        output (our own product) is removed and the failure names the
        reason - never fill a shared disk."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "out.mkv"
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            out = out_dir / "out.mkv"
            src.write_bytes(b"x" * 8)
            # a stub "HandBrake" that writes a partial file and emits percent
            # lines forever (until killed)
            if os.name == "nt":
                script = out_dir / "hb_stub.cmd"
                script.write_bytes(
                    b"@echo off\r\n"
                    b":loop\r\n"
                    b"echo Encoding: task 1 of 1, 42.31 %%\r\n"
                    b"timeout /t 1 /nobreak >nul\r\n"
                    b"goto loop\r\n"
                )
                command = [str(script)]
            else:
                script = out_dir / "hb_stub.sh"
                script.write_bytes(
                    b'#!/bin/sh\nwhile true; do echo "Encoding: task 1 of 1, 42.31 %"; sleep 1; done\n'
                )
                script.chmod(0o755)
                command = [str(script)]
            with mock.patch(
                "shrinkray.engine.pipeline.disk_free_bytes", return_value=1_000_000
            ):  # ~1 MB: below the 1 GiB floor
                result = engine_pipeline._encode_with_command(
                    src, out, command, progress=lambda p: None
                )
            self.assertFalse(result.success)
            self.assertTrue(result.cancelled)
            self.assertIn("disk space", result.error or "")
            # the partial output was removed by the engine itself
            self.assertFalse(out.exists())

    def test_disk_watchdog_ignores_unreadable_drive(self):
        """An unreadable drive (disk_free_bytes None) never crashes or
        cancels an encode - the watchdog degrades gracefully."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            out = Path(tmp) / "out.mkv"
            src.write_bytes(b"x" * 8)
            command = ["cmd", "/c", "exit 0"] if os.name == "nt" else ["true"]
            with mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None):
                result = engine_pipeline._encode_with_command(
                    src, out, command, progress=lambda p: None
                )
            # no cancellation: the watchdog stayed out of the way
            self.assertFalse(result.cancelled)
            self.assertIn(result.success or result.error == "no output file was written", (True,))


class TestEncodeLock(unittest.TestCase):
    """The per-output-dir encode slot: collision-proof, stale-aware."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name).resolve() / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_and_release(self):
        from shrinkray.engine.lock import guard_encodes

        with guard_encodes(self.out, holder="test") as lock:
            self.assertTrue(lock.is_file())
            # pid-stamped, and the holder is named for the busy message
            fields = lock.read_text().split()
            self.assertEqual(int(fields[0]), os.getpid())
            self.assertEqual(fields[1], "test")
            self.assertEqual(len(fields), 3, "the ownership token is written")
        self.assertFalse(lock.exists())

    def test_second_claimant_is_rejected(self):
        from shrinkray.engine.lock import EncodeBusyError, guard_encodes

        with guard_encodes(self.out), self.assertRaises(EncodeBusyError) as ctx:
            guard_encodes(self.out).__enter__()
        self.assertIn("pid", str(ctx.exception))
        self.assertIn("heartbeat", str(ctx.exception))

    def test_a_silent_lock_is_stolen(self):
        """A lock whose holder stopped beating is reclaimed (issue #383):
        the window measures silence, never the pid or the run's age."""
        from shrinkray.engine.lock import _LOCK_STALE_SECONDS, _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("999999999")
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertEqual(acquired, lock)
        self.assertEqual(int(lock.read_text().split()[0]), os.getpid())
        lock.unlink()

    def test_a_steal_the_filesystem_refuses_is_reported_not_called_busy(self):
        """A rename the filesystem refuses (a read-only library, EBUSY) is not
        "another encoder": reporting it as one names a phantom holder and hides
        the real fault (PR #389's review finding 4, invariant 5)."""
        from shrinkray.engine import lock as lock_mod

        self.out.mkdir(parents=True, exist_ok=True)
        lock = lock_mod._lock_path(self.out)
        lock.write_text("999999999 encode 0123456789ab")
        old = time.time() - lock_mod._LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        with (
            mock.patch.object(lock_mod.os, "rename", side_effect=OSError("read-only")),
            mock.patch.object(lock_mod, "log_error") as error,
            self.assertRaises(lock_mod.EncodeBusyError) as ctx,
        ):
            lock_mod.acquire_encode_slot(self.out, holder="test")
        self.assertIn("could not take the stale encode lock", str(ctx.exception))
        self.assertNotIn("another encoder is already using", str(ctx.exception))
        messages = " ".join(str(c.args[0]) for c in error.call_args_list)
        self.assertIn("read-only", messages)

    def test_a_failed_release_is_reported(self):
        """A release that cannot unlink is logged, not swallowed: the next encode
        of that directory would be refused for the whole stale window with no
        explanation (PR #389's review finding 5)."""
        from shrinkray.engine import lock as lock_mod

        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("busy")),
            mock.patch.object(lock_mod, "log_error") as error,
            lock_mod.guard_encodes(self.out, holder="test"),
        ):
            pass
        messages = " ".join(str(c.args[0]) for c in error.call_args_list)
        self.assertIn("Could not release the encode lock", messages)

    def test_a_live_holder_in_another_pid_namespace_keeps_the_lock(self):
        """Issue #383's acceptance criterion: two holders whose pids collide
        (the server and a one-shot container are both pid 1) must still
        exclude each other. The heartbeat decides, so the pid is irrelevant.
        """
        from shrinkray.engine.lock import EncodeBusyError, _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        # a holder in another namespace: pid 1, which is NOT us here
        lock.write_text("1 serve")
        with self.assertRaises(EncodeBusyError):
            acquire_encode_slot(self.out, holder="test")
        self.assertTrue(lock.exists())
        lock.unlink()

    def test_a_silent_lock_from_a_live_pid_is_reclaimed(self):
        """The pid is not the test: a lock naming a process that IS alive
        (ours) but has not beaten for the stale window is reclaimed, so a
        SIGKILLed run's lock cannot outlive it for hours (issue #383)."""
        from shrinkray.engine.lock import _LOCK_STALE_SECONDS, _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text(f"{os.getpid()} encode")
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertEqual(acquired, lock)
        self.assertEqual(int(lock.read_text().split()[0]), os.getpid())
        lock.unlink()

    def test_an_old_pid_only_lock_is_reclaimed_by_its_mtime(self):
        """Issue #383's upgrade criterion: a lock written before this change
        carries only a pid - and that pid may be ALIVE (the restarted server is
        pid 1 again). It is judged by its mtime like any other, so an upgrade
        never keeps a dead run's lock for the old six-hour window."""
        from shrinkray.engine.lock import _LOCK_STALE_SECONDS, _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        # a LIVE pid (ours) in the old, token-less format: pid-only locks must
        # not be honoured on liveness alone
        lock.write_text(str(os.getpid()))
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        self.assertEqual(acquire_encode_slot(self.out, holder="test"), lock)
        lock.unlink()

    def test_the_heartbeat_keeps_a_held_slot_alive(self):
        """The holder touches the lock while it runs, so the stale window
        measures silence: a slot held past the window is still ours
        (issue #383)."""
        from shrinkray.engine import lock as lock_mod

        with (
            mock.patch.object(lock_mod, "_HEARTBEAT_SECONDS", 0.01),
            mock.patch.object(lock_mod, "_LOCK_STALE_SECONDS", 0.05),
            lock_mod.guard_encodes(self.out, holder="test") as held,
        ):
            time.sleep(0.2)
            with self.assertRaises(lock_mod.EncodeBusyError):
                lock_mod.acquire_encode_slot(self.out, holder="rival")
        self.assertFalse(held.exists())

    def test_a_read_error_is_not_a_takeover(self):
        """A lock that cannot be READ (an EIO or EACCES on a NAS mount) must not
        be reported as a rival taking the slot: the beat says "failed", warns,
        and keeps beating, so the holder's own lease is not abandoned on a
        transient filesystem error (PR #409's review finding 1)."""
        from shrinkray.engine import lock as lock_mod

        with (
            mock.patch.object(lock_mod, "_HEARTBEAT_SECONDS", 0.01),
            mock.patch.object(lock_mod, "log_error") as error,
            mock.patch.object(lock_mod, "log_warning") as warning,
            mock.patch.object(lock_mod, "_read_fields", return_value=None),
            lock_mod.guard_encodes(self.out, holder="test") as held,
        ):
            time.sleep(0.2)
        errors = " ".join(str(c.args[0]) for c in error.call_args_list)
        warnings = " ".join(str(c.args[0]) for c in warning.call_args_list)
        self.assertNotIn("Another encoder took the slot", errors)
        self.assertIn("Could not refresh the encode lock", warnings)
        self.assertTrue(held.exists())

    def test_a_failed_heartbeat_is_reported(self):
        """A beat that cannot write is logged, never swallowed: the holder must
        learn its slot is on a timer (invariant 5)."""
        from shrinkray.engine import lock as lock_mod

        with (
            mock.patch.object(lock_mod, "_HEARTBEAT_SECONDS", 0.01),
            mock.patch.object(lock_mod, "log_warning") as warning,
            mock.patch.object(lock_mod.os, "utime", side_effect=OSError("read-only")),
            lock_mod.guard_encodes(self.out, holder="test"),
        ):
            time.sleep(0.2)
        messages = " ".join(str(c.args[0]) for c in warning.call_args_list)
        self.assertIn("Could not refresh the encode lock", messages)

    def test_a_beat_never_refreshes_a_lock_that_is_no_longer_ours(self):
        """A holder that lost its slot must not keep the rival's lease alive:
        the beat compares the token first, reports the loss as an error, and
        stops (PR #389's review finding 2)."""
        from shrinkray.engine import lock as lock_mod

        with (
            mock.patch.object(lock_mod, "_HEARTBEAT_SECONDS", 0.01),
            mock.patch.object(lock_mod, "log_error") as error,
            lock_mod.guard_encodes(self.out, holder="test") as held,
        ):
            # a rival steals the slot and writes its own stamp
            held.write_text("1 rival fedcba987654")
            time.sleep(0.2)
            self.assertEqual(
                held.read_text(), "1 rival fedcba987654", "the rival's lease was refreshed"
            )
        messages = " ".join(str(c.args[0]) for c in error.call_args_list)
        self.assertIn("Another encoder took the slot", messages)

    def test_garbage_lock_content_is_busy_until_it_goes_silent(self):
        """Unreadable content is not evidence of a dead holder: the mtime is
        (issue #383). A fresh garbage file is busy; a silent one is stolen."""
        from shrinkray.engine.lock import (
            _LOCK_STALE_SECONDS,
            EncodeBusyError,
            _lock_path,
            acquire_encode_slot,
        )

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("not-a-pid")
        with self.assertRaises(EncodeBusyError):
            acquire_encode_slot(self.out)
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        self.assertEqual(acquire_encode_slot(self.out), lock)
        lock.unlink()

    def test_the_steal_loses_to_a_heartbeat_that_lands_first(self):
        """Between judging a lock stale and taking it, the holder may beat
        again (PR #389's review finding 4). The steal restores the lock, never
        discarding a live holder's slot."""
        from shrinkray.engine import lock as lock_mod
        from shrinkray.engine.lock import _LOCK_STALE_SECONDS, EncodeBusyError, _lock_path

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("999999999 encode 0123456789ab")
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))

        real_silence = lock_mod._silence_seconds
        calls = {"n": 0}

        def raced(path):
            calls["n"] += 1
            if calls["n"] == 2:
                # the holder beat between the judgement and the steal
                os.utime(path, None)
            return real_silence(path)

        with (
            mock.patch.object(lock_mod, "_silence_seconds", side_effect=raced),
            self.assertRaises(EncodeBusyError),
        ):
            lock_mod.acquire_encode_slot(self.out, holder="test")
        self.assertTrue(lock.exists(), "the live holder's lock was not restored")
        lock.unlink()

    def test_two_claimants_judging_the_same_lock_only_one_wins(self):
        """Two claimants that both read the same stale lock must not each
        delete the other's brand-new lock (PR #389's review finding 4). Both
        judge it stale here; the steal is a rename, so exactly one wins it and
        the other raises instead of unlinking a live lock."""
        from shrinkray.engine import lock as lock_mod
        from shrinkray.engine.lock import _LOCK_STALE_SECONDS, EncodeBusyError, _lock_path

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("999999999 encode 0123456789ab")
        old = time.time() - _LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))

        real_silence = lock_mod._silence_seconds

        def always_stale(path):
            # both claimants read the SAME stale stamp before either takes it
            return real_silence(path) if path != lock else _LOCK_STALE_SECONDS + 60

        with mock.patch.object(lock_mod, "_silence_seconds", side_effect=always_stale):
            first = lock_mod.acquire_encode_slot(self.out, holder="first")
            self.assertEqual(first, lock)
            # the second claimant judged the same stamp stale: the rename loses
            # (the file it read is gone), so it never deletes the first's lock
            with self.assertRaises(EncodeBusyError):
                lock_mod.acquire_encode_slot(self.out, holder="second")
            self.assertTrue(lock.exists(), "the winner's lock was deleted")
            self.assertEqual(lock.read_text().split()[1], "first")
        lock.unlink()

    def test_a_lost_holder_does_not_release_the_new_holders_lock(self):
        """A holder that stalled past the window must not delete the new
        holder's lock when it finally exits (issue #383). The pid cannot tell
        them apart - the server and a one-shot container are both pid 1 - so
        the token written at acquisition decides.

        This acquires through the API first, so the token comparison is reached:
        the missing-_OWNED early return is a different path."""
        from shrinkray.engine.lock import acquire_encode_slot, release_encode_slot

        acquired = acquire_encode_slot(self.out, holder="test")
        # a rival in another namespace takes the slot and writes its own stamp
        acquired.write_text("1 encode-job 0123456789ab")
        release_encode_slot(acquired)
        self.assertTrue(acquired.exists(), "the rival's lock was deleted")
        self.assertEqual(acquired.read_text(), "1 encode-job 0123456789ab")
        acquired.unlink()

    def test_a_holder_whose_lock_was_stolen_does_not_delete_the_new_one(self):
        """This process acquires, a rival steals the silent lock and writes its
        own, and then we exit: release must leave the rival's lock alone. The
        pid is identical in the container, so the token decides (issue #383)."""
        from shrinkray.engine.lock import _lock_path, acquire_encode_slot, release_encode_slot

        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertEqual(acquired, _lock_path(self.out))
        # the rival stole it and wrote its own stamp (pid 1, a different token)
        acquired.write_text("1 encode-job fedcba987654")
        release_encode_slot(acquired)
        self.assertTrue(acquired.exists(), "the rival's lock was deleted")
        self.assertEqual(acquired.read_text().split()[2], "fedcba987654")
        acquired.unlink()

    def test_releasing_our_own_lock_removes_it(self):
        """The positive case of the token check: our own lock is removed."""
        from shrinkray.engine.lock import acquire_encode_slot, release_encode_slot

        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertTrue(acquired.exists())
        release_encode_slot(acquired)
        self.assertFalse(acquired.exists())

    def test_quality_progress_callback_maps_percent(self):
        """The CLI progress callback maps engine percent events onto the
        rich task (encoding description + completed percent); probe step
        events update the description only (the task's total is the movie
        count, so a probe must not move the bar)."""
        from shrinkray.__main__ import _quality_progress_callback

        updates: list[dict] = []

        class FakeProgress:
            def update(self, task, **kwargs):
                updates.append(kwargs)

        callback = _quality_progress_callback(FakeProgress(), "task1")
        callback({"stage": "probe", "detail": "probe encode (CRF 22)"})
        callback({"stage": "encode", "percent": 42.31, "movie": "m.mkv"})
        self.assertEqual(
            updates,
            [
                {"description": "Probing: probe encode (CRF 22)"},
                {"description": "Encoding 'm.mkv'", "completed": 42.31},
            ],
        )

    def test_encode_quality_cli_passes_progress(self):
        """The CLI quality path hands a progress callback to the engine (a
        4-hour encode must not be silent)."""
        import tempfile

        from shrinkray.__main__ import _record_encodes
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db_file = root / "metadata.db"
            connection = db.connect(db_file)
            try:
                captured: dict = {}

                def fake_quality(movie_in, output_file, **kwargs):
                    captured["progress"] = kwargs.get("progress")
                    captured["encoder_preset"] = kwargs.get("encoder_preset")
                    return QualityResult(
                        input=movie_in,
                        output=output_file,
                        success=True,
                        input_size=100,
                        output_size=50,
                    )

                with (
                    mock.patch(
                        "shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_quality
                    ),
                    mock.patch("shrinkray.__main__.record_quality_results"),
                ):
                    _record_encodes(
                        connection,
                        [movie],
                        root / "enc",
                        config_helper.load_config(),
                        dry_run=False,
                        overwrite=False,
                        quality=True,
                        target_vmaf=95.0,
                        verify=False,
                        threads=None,
                        db_file=db_file,
                        encoder_preset="6",
                    )
                # the CLI passed a callable, and calling it does not crash
                self.assertTrue(callable(captured["progress"]))
                # the CLI's --encoder-preset reaches the engine (issue #31:
                # the preset was a constant no caller could override)
                self.assertEqual(captured["encoder_preset"], "6")
                captured["progress"]({"stage": "encode", "percent": 10.0, "movie": "movie.mkv"})
            finally:
                connection.close()

    def test_encode_quality_cli_passes_the_language_filters(self):
        """The CLI quality path hands the config's language filters to the
        engine (issue #240): _record_encodes reads them from the loaded
        config, never a literal."""
        import tempfile

        from shrinkray.__main__ import _record_encodes
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db_file = root / "metadata.db"
            connection = db.connect(db_file)
            try:
                captured: dict = {}

                def fake_quality(movie_in, output_file, **kwargs):
                    captured["audio_languages"] = kwargs.get("audio_languages")
                    captured["subtitle_languages"] = kwargs.get("subtitle_languages")
                    return QualityResult(
                        input=movie_in,
                        output=output_file,
                        success=True,
                        input_size=100,
                        output_size=50,
                    )

                with (
                    mock.patch(
                        "shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_quality
                    ),
                    mock.patch("shrinkray.__main__.record_quality_results"),
                ):
                    _record_encodes(
                        connection,
                        [movie],
                        root / "enc",
                        config_helper.load_config(),
                        dry_run=False,
                        overwrite=False,
                        quality=True,
                        target_vmaf=95.0,
                        verify=False,
                        threads=None,
                        db_file=db_file,
                        encoder_preset="6",
                    )
                # the config's lists reach the engine (loaded from config,
                # not restated in the CLI); the DEFAULT config is empty —
                # every track kept (issue #297's decision) — so the CLI
                # passes no filter lists at all
                self.assertEqual(captured["audio_languages"], [])
                self.assertEqual(captured["subtitle_languages"], [])
            finally:
                connection.close()

    def test_build_vmaf_command_subsample(self):
        """n_subsample lands in the filtergraph only when > 1 (None/0/1
        measure every frame)."""
        from shrinkray.tools.vmaf import build_vmaf_command

        cmd = build_vmaf_command(Path("d.mkv"), Path("r.mkv"), 4, style="modern", subsample=10)
        self.assertIn("n_subsample=10", " ".join(cmd))
        plain = build_vmaf_command(Path("d.mkv"), Path("r.mkv"), 4, style="modern")
        self.assertNotIn("n_subsample", " ".join(plain))
        one = build_vmaf_command(Path("d.mkv"), Path("r.mkv"), 4, style="modern", subsample=1)
        self.assertNotIn("n_subsample", " ".join(one))

    def test_vmaf_model_follows_ab_av1_thresholds(self):
        """Issue #309: the 4K model above 2560x1440 (both axes), the 1080p
        model otherwise - ab-av1's rule, read at v0.11.7. The dimensions are
        the POST-CROP ones, so a 4K scope film cropped to 3840x1390 stays on
        the 1080p model exactly as ab-av1 would score it."""
        from shrinkray.tools.vmaf import vmaf_model

        # below or at either threshold => 1080p model
        self.assertEqual(vmaf_model(1920, 1080), "vmaf_v0.6.1.json")
        self.assertEqual(vmaf_model(2560, 1440), "vmaf_v0.6.1.json")
        self.assertEqual(vmaf_model(2561, 1440), "vmaf_v0.6.1.json")
        self.assertEqual(vmaf_model(2560, 1441), "vmaf_v0.6.1.json")
        # above BOTH => 4K model
        self.assertEqual(vmaf_model(2561, 1441), "vmaf_4k_v0.6.1.json")
        self.assertEqual(vmaf_model(3840, 2160), "vmaf_4k_v0.6.1.json")
        # 4K scope: wide enough, too short => 1080p model (ab-av1's edge)
        self.assertEqual(vmaf_model(3840, 1390), "vmaf_v0.6.1.json")
        # unknown dimensions never guess the 4K model
        self.assertEqual(vmaf_model(None, 2160), "vmaf_v0.6.1.json")
        self.assertEqual(vmaf_model(3840, None), "vmaf_v0.6.1.json")

    def test_vmaf_auto_scale_follows_ab_av1_thresholds(self):
        """Issue #309: ab-av1's --vmaf-scale auto. The 1080p model upscales
        below 1728x972 towards 1080p, the 4K model below 3456x1944 towards
        4K, minimally on one axis so the aspect ratio is kept."""
        from shrinkray.tools.vmaf import vmaf_auto_scale

        # at or above a threshold: no scaling
        self.assertIsNone(vmaf_auto_scale("vmaf_v0.6.1.json", 1920, 1080))
        self.assertIsNone(vmaf_auto_scale("vmaf_v0.6.1.json", 1728, 972))
        self.assertIsNone(vmaf_auto_scale("vmaf_4k_v0.6.1.json", 3840, 2160))
        self.assertIsNone(vmaf_auto_scale("vmaf_4k_v0.6.1.json", 3456, 1944))
        # below both: the proportionally further axis is scaled, the other -1
        self.assertEqual(vmaf_auto_scale("vmaf_v0.6.1.json", 1280, 720), (1920, -1))
        self.assertEqual(vmaf_auto_scale("vmaf_4k_v0.6.1.json", 3008, 1692), (3840, -1))
        self.assertEqual(vmaf_auto_scale("vmaf_v0.6.1.json", 640, 480), (-1, 1080))
        # unknown dimensions: no scaling rather than a guess
        self.assertIsNone(vmaf_auto_scale("vmaf_v0.6.1.json", None, 1080))

    def test_vmaf_recipe_uses_the_cropped_dimensions(self):
        """Issue #308/#309: the model and scale follow the dimensions the
        ENCODER produces, i.e. the source minus the crop - the distorted side
        of the comparison is already cropped by HandBrake."""
        from shrinkray.tools.vmaf import vmaf_recipe

        # 3840x2160 with 140+140 vertical bars => 3840x1880: still 4K
        self.assertEqual(
            vmaf_recipe(crop=(140, 140, 0, 0), width=3840, height=2160).model,
            "vmaf_4k_v0.6.1.json",
        )
        # the same source cropped to 3840x1390 is NOT 4K by ab-av1's rule
        self.assertEqual(
            vmaf_recipe(crop=(385, 385, 0, 0), width=3840, height=2160).model,
            "vmaf_v0.6.1.json",
        )

    def test_vmaf_recipe_pixel_format_follows_bit_depth(self):
        """Issue #308: one explicit format for both inputs, 10-bit for a
        10-bit source (dropping the two extra bits would score a conversion
        the encode never made)."""
        from shrinkray.tools.vmaf import vmaf_pix_fmt, vmaf_recipe

        self.assertEqual(vmaf_pix_fmt(10), "yuv420p10le")
        self.assertEqual(vmaf_pix_fmt(12), "yuv420p10le")
        self.assertEqual(vmaf_pix_fmt(8), "yuv420p")
        self.assertEqual(vmaf_pix_fmt(None), "yuv420p")
        self.assertEqual(vmaf_recipe(bit_depth=10).pix_fmt, "yuv420p10le")

    def test_vmaf_recipe_identifier_covers_every_score_moving_input(self):
        """Issue #308: the recipe identifier is the cache key's measurement
        half, so each field that can move a score must change it - and the plan
        fingerprint must stay stable across the crop, which it cannot know."""
        from shrinkray.tools.vmaf import vmaf_recipe

        base = vmaf_recipe(width=1920, height=1080, bit_depth=8)
        self.assertIn("model=vmaf_v0.6.1.json", base.identifier())
        self.assertIn("fmt=yuv420p", base.identifier())
        self.assertIn("hdr=no", base.identifier())
        # a different model / pixel format / tone-map => a different identifier
        self.assertNotEqual(base.identifier(), vmaf_recipe(width=3840, height=2160).identifier())
        self.assertNotEqual(base.identifier(), vmaf_recipe(bit_depth=10).identifier())
        self.assertNotEqual(base.identifier(), vmaf_recipe(hdr=True).identifier())
        # the plan fingerprint ignores the crop-dependent model/scale: the plan
        # lookup runs before bar detection and cannot know them
        self.assertEqual(
            base.fingerprint(),
            vmaf_recipe(crop=(140, 140, 0, 0), width=1920, height=1080).fingerprint(),
        )
        self.assertNotEqual(base.fingerprint(), vmaf_recipe(bit_depth=10).fingerprint())

    def test_build_vmaf_command_rebases_timestamps_and_syncs(self):
        """Issue #308: ab-av1's alignment. Both inputs are rebased to PTS 0
        with a common timebase and paired by nearest timestamp, so a probe
        sample cut before -i on an open-GOP source cannot pair frames one or
        two apart (which silently lowers every score)."""
        from shrinkray.tools.vmaf import build_vmaf_command

        command = build_vmaf_command(Path("d.mkv"), Path("r.mkv"), 4, style="modern")
        joined = " ".join(command)
        self.assertIn("setpts=PTS-STARTPTS", joined)
        self.assertIn("settb=AVTB", joined)
        self.assertIn("shortest=true", joined)
        self.assertIn("ts_sync_mode=nearest", joined)
        # one explicit pixel format on BOTH inputs, before the sync
        self.assertEqual(joined.count("format=yuv420p"), 2)
        self.assertIn("[0:v]format=yuv420p,setpts=PTS-STARTPTS,settb=AVTB[d]", joined)
        self.assertIn("[1:v]format=yuv420p,setpts=PTS-STARTPTS,settb=AVTB[r]", joined)

    def test_build_vmaf_command_10bit_formats_both_inputs(self):
        """Issue #308: a 10-bit source converts BOTH sides to 10-bit, so the
        comparison is not an 8-bit conversion of one side only."""
        from shrinkray.tools.vmaf import build_vmaf_command, vmaf_recipe

        command = build_vmaf_command(
            Path("d.mkv"),
            Path("r.mkv"),
            4,
            style="modern",
            recipe=vmaf_recipe(bit_depth=10),
        )
        joined = " ".join(command)
        self.assertEqual(joined.count("format=yuv420p10le"), 2)
        self.assertNotIn("format=yuv420p,", joined)

    def test_build_vmaf_command_4k_uses_the_4k_model(self):
        """Issue #309: a 4K measurement names the 4K model in the
        filtergraph (the shipped file, by bare filename like the 1080p one)."""
        from shrinkray.tools.vmaf import build_vmaf_command, vmaf_recipe

        command = build_vmaf_command(
            Path("d.mkv"),
            Path("r.mkv"),
            4,
            style="modern",
            recipe=vmaf_recipe(width=3840, height=2160),
        )
        self.assertIn("model=path=vmaf_4k_v0.6.1.json", " ".join(command))

    def test_build_vmaf_command_scales_both_inputs(self):
        """Issue #309: an auto scale applies to BOTH inputs, after the
        reference's crop (so the scale target matches the cropped geometry)."""
        from shrinkray.tools.vmaf import build_vmaf_command, vmaf_recipe

        recipe = vmaf_recipe(crop=(140, 140, 0, 0), width=1280, height=720)
        command = build_vmaf_command(
            Path("d.mkv"), Path("r.mkv"), 4, style="modern", crop=(140, 140, 0, 0), recipe=recipe
        )
        joined = " ".join(command)
        self.assertEqual(joined.count("scale=1920:-1:flags=bicubic"), 2)
        # the reference crops first, then scales
        self.assertIn("crop=iw-0:ih-280:0:140,format=yuv420p,scale=1920:-1:flags=bicubic", joined)

    def test_vmaf_model_and_4k_model_ship_as_package_data(self):
        """Both models are shipped (old libvmaf cannot load embedded models)
        and the 4K file is the model the identifier names."""
        from shrinkray.tools.vmaf import VMAF_4K_MODEL, VMAF_MODEL, vmaf_model

        self.assertTrue(VMAF_MODEL.is_file())
        self.assertTrue(VMAF_4K_MODEL.is_file())
        self.assertEqual(VMAF_4K_MODEL.name, vmaf_model(3840, 2160))

    def test_encode_with_quality_forwards_crop_sample_count(self):
        """The encode re-detects the crop with the SAME window count the
        plan fingerprint carries (issue #327): the parameter existed on
        _probe_movie and in the fingerprint, but the encode path never
        forwarded it - --crop-samples 3 fingerprinted 3 and measured 10, so
        the fallback detector could decide a different crop than the plan
        predicted.
        """
        import tempfile

        from shrinkray.engine.pipeline import encode_with_quality

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out_dir = root / "enc"
            out_dir.mkdir()
            out = out_dir / "in.mkv"
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho x > %~4\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho x > "$4"\nexit 0\n')
                stub.chmod(0o755)
            with (
                mock.patch(
                    "shrinkray.engine.pipeline._probe_movie", return_value=(20, 95.2, [], None)
                ) as probe,
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None),
            ):
                encode_with_quality(src, out, progress=None, crop_sample_count=3)
            self.assertEqual(probe.call_args.kwargs.get("crop_sample_count"), 3)

    def test_encode_with_quality_verify_runs_measurement(self):
        """verify=True measures the OUTPUT against the SOURCE after the
        encode; a measurement failure is non-fatal (the encode stands)."""
        import tempfile

        from shrinkray.engine.pipeline import encode_with_quality

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out_dir = root / "enc"
            out_dir.mkdir()
            out = out_dir / "in.mkv"
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho x > %~4\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho x > "$4"\nexit 0\n')
                stub.chmod(0o755)

            measured: list = []

            def fake_measure(distorted, reference, workdir, **kwargs):
                measured.append((distorted, reference, kwargs.get("subsample")))
                return 94.5

            with (
                mock.patch(
                    "shrinkray.engine.pipeline._probe_movie", return_value=(20, 95.2, [], None)
                ),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch("shrinkray.engine.pipeline.measure_vmaf", side_effect=fake_measure),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None),
            ):
                result = encode_with_quality(src, out, progress=None, verify=True)
            self.assertTrue(result.success)
            # measured OUTPUT vs SOURCE
            self.assertEqual(len(measured), 1)
            self.assertEqual(measured[0][0], out)
            self.assertEqual(measured[0][1], src)
            self.assertEqual(result.verified_vmaf, 94.5)

    def test_encode_with_quality_verify_failure_nonfatal(self):
        """A failed verification never invalidates the encode (it measured
        fine at probe time): success stays true, verified_vmaf stays None."""
        import tempfile

        from shrinkray.engine.pipeline import encode_with_quality

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out_dir = root / "enc"
            out_dir.mkdir()
            out = out_dir / "in.mkv"
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho x > %~4\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho x > "$4"\nexit 0\n')
                stub.chmod(0o755)
            with (
                mock.patch(
                    "shrinkray.engine.pipeline._probe_movie", return_value=(20, 95.2, [], None)
                ),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch(
                    "shrinkray.engine.pipeline.measure_vmaf",
                    side_effect=RuntimeError("ffmpeg exploded"),
                ),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None),
            ):
                result = encode_with_quality(src, out, progress=None, verify=True)
            self.assertTrue(result.success)
            self.assertIsNone(result.verified_vmaf)

    def test_encode_with_quality_verify_without_ffmpeg_stands(self):
        """CI regression (run 160): ffmpeg is absent, the verification
        already failed non-fatally - and then result construction crashed
        anyway because _tool_versions() probed ffmpeg unguarded. Version
        metadata must never fail an encode."""
        import tempfile

        from shrinkray.engine.pipeline import encode_with_quality

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out_dir = root / "enc"
            out_dir.mkdir()
            out = out_dir / "in.mkv"
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho x > %~4\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho x > "$4"\nexit 0\n')
                stub.chmod(0o755)
            with (
                mock.patch(
                    "shrinkray.engine.pipeline._probe_movie", return_value=(20, 95.2, [], None)
                ),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch(
                    "shrinkray.engine.pipeline.measure_vmaf", side_effect=RuntimeError("no ffmpeg")
                ),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None),
                mock.patch(
                    "shrinkray.tools.vmaf.detect_vmaf_style",
                    side_effect=RuntimeError("ffmpeg not found"),
                ),
            ):
                result = encode_with_quality(src, out, progress=None, verify=True)
            self.assertTrue(result.success)
            self.assertIsNone(result.verified_vmaf)

    def test_tool_versions_omits_libvmaf_without_ffmpeg(self):
        """_tool_versions degrades to the handbrake token (or an empty
        string) when ffmpeg is absent - no exception."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "hb.cmd"
            hb.write_bytes(b"@echo off\r\necho HandBrake 1.11.2\r\n")
            with mock.patch(
                "shrinkray.tools.vmaf.detect_vmaf_style",
                side_effect=RuntimeError("ffmpeg not found"),
            ):
                versions = tools_vmaf._tool_versions(str(hb))
            self.assertNotIn("libvmaf=", versions)

    def test_check_disk_space_insufficient(self):
        """free < required => ok=False with a descriptive message."""
        import tempfile

        from shrinkray.engine.pipeline import check_disk_space

        with tempfile.TemporaryDirectory() as tmp:
            ok, message = check_disk_space(Path(tmp), 10**15)  # 1 PB: never free
            self.assertFalse(ok)
            self.assertIn("not enough disk space", message)
            self.assertIn("worst case", message)

    def test_check_disk_space_plenty(self):
        """free >> required => ok=True, empty message."""
        import tempfile

        from shrinkray.engine.pipeline import check_disk_space

        with tempfile.TemporaryDirectory() as tmp:
            ok, message = check_disk_space(Path(tmp), 1)  # 1 byte
            self.assertTrue(ok)
            self.assertEqual(message, "")

    def test_check_disk_space_unreadable_degrades(self):
        """An unreadable drive must not block encodes (graceful degrade)."""
        import tempfile

        from shrinkray.engine.pipeline import check_disk_space

        with tempfile.TemporaryDirectory() as tmp:
            # a path that cannot exist as a directory (file in the way)
            blocker = Path(tmp) / "blocker"
            blocker.write_bytes(b"x")
            ok, _message = check_disk_space(blocker / "sub", 10)
            self.assertTrue(ok)  # degraded, not failed
            self.assertIn("could not be checked", _message)


class TestSubprocessTimeouts(unittest.TestCase):
    """probe_duration_seconds turns a TimeoutExpired into its documented
    failure path (None, logged) instead of hanging forever (issue #62: a
    stale NFS/SMB mount blocks the kernel read with no timeout)."""

    def test_probe_duration_timeout_returns_none_and_logs(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch(
                    "shrinkray.tools.ffprobe.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
                ),
                patch("shrinkray.tools.ffprobe.log") as logged,
            ):
                self.assertIsNone(tools_ffprobe.probe_duration_seconds(movie))
            self.assertTrue(logged.called)
            self.assertIn("movie.mkv", logged.call_args.args[0])
            self.assertIn("60", logged.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
