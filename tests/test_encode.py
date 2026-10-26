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

    def test_build_command(self):
        command = tools_handbrake.build_command(
            "C:/HandBrake/HandBrakeCLI.exe",
            Path("in.mkv"),
            Path("out.mkv"),
            "Fast 1080p30",
            "svt_av1",
        )
        self.assertEqual(command[0], "C:/HandBrake/HandBrakeCLI.exe")
        self.assertIn("--input", command)
        self.assertIn("in.mkv", command)
        self.assertIn("--output", command)
        self.assertIn("out.mkv", command)
        self.assertIn("Fast 1080p30", command)
        self.assertIn("svt_av1", command)
        # audio passthrough without conflicting options
        self.assertIn("--aencoder", command)
        self.assertNotIn("--audio", command)
        self.assertIn("--optimize", command)

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
        as an option. The check is on the HandBrake commands only: the
        ffmpeg sample extraction uses libx264 for intermediate clips, which
        is not an output codec.
        """
        fixed = tools_handbrake.build_command(
            "hb",
            Path("in.mkv"),
            Path("out.mkv"),
            "Fast 1080p30",
            engine_encoders.encoder_for(8, False),
        )
        for command in (
            fixed,
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

    def test_build_command_with_thread_cap(self):
        command = tools_handbrake.build_command(
            "hb",
            Path("in.mkv"),
            Path("out.mkv"),
            "Fast 1080p30",
            "svt_av1",
            encoder_threads=8,
        )
        self.assertIn("--encopts", command)
        self.assertIn("pin=8", command)
        # uncapped: no encopts at all
        plain = tools_handbrake.build_command(
            "hb", Path("in.mkv"), Path("out.mkv"), "Fast 1080p30", "svt_av1"
        )
        self.assertNotIn("--encopts", plain)

    def test_encode_movies_max_threads_tightens_share(self):
        """The configured cap wins when tighter than the parallel CPU share."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            with (
                tempfile.TemporaryDirectory() as out,
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch("shrinkray.engine.pipeline._encode_with_command") as enc,
            ):
                enc.side_effect = lambda i, o, cmd, progress=None, db_file=None: (
                    encode.EncodeResult(input=i, output=o, success=True)
                )
                # workers=2 on 16 cores would give a big share; the cap must win
                engine_pipeline.encode_movies(
                    [source],
                    Path(out),
                    preset="Fast 1080p30",
                    encoder="svt_av1",
                    workers=2,
                    max_threads=4,
                )
                command = enc.call_args[0][2]
        self.assertIn("--encopts", command)
        self.assertIn("pin=4", command)

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

    def test_encode_file_missing_input(self):
        with self.assertRaises(FileNotFoundError):
            engine_pipeline.encode_file(Path("does-not-exist.mkv"), Path("out.mkv"))

    def test_encode_movies_dry_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "encoded"
            results = engine_pipeline.encode_movies(
                [Path("a.mkv"), Path("b.mkv")], out_dir, dry_run=True
            )
            self.assertEqual(len(results), 0)
            # dry run => no output directory created
            self.assertFalse(out_dir.exists())

    def test_encode_movies_skip_existing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "a.mkv").write_bytes(b"x")
            results = engine_pipeline.encode_movies([Path("a.mkv")], out_dir)
            # skipped => no result recorded
            self.assertEqual(len(results), 0)

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

    def test_encode_movies_rich_bar_survives_percent_lines(self):
        """Fixed mode's rich bar wiring: the engine gets a CALLABLE (not the
        rich Progress object) - the first HandBrake percent line used to
        raise TypeError: 'Progress' object is not callable."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mkv"
            src.write_bytes(b"x" * 8)
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            stub = out_dir / "hb_stub.cmd" if os.name == "nt" else out_dir / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho Encoding: task 1 of 1, 42.31 %%\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho "Encoding: task 1 of 1, 42.31 %"\nexit 0\n')
                stub.chmod(0o755)
            with (
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
            ):
                # must not raise TypeError; the encode result is failure
                # (stub writes no output) but the crash is the regression
                results = engine_pipeline.encode_movies([src], out_dir)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].success)
            self.assertEqual(results[0].error, "no output file was written")

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
            # pid-stamped (+ the process start time where the platform has one)
            self.assertEqual(int(lock.read_text().split()[0]), os.getpid())
        self.assertFalse(lock.exists())

    def test_second_claimant_is_rejected(self):
        from shrinkray.engine.lock import EncodeBusyError, guard_encodes

        with guard_encodes(self.out), self.assertRaises(EncodeBusyError) as ctx:
            guard_encodes(self.out).__enter__()
        self.assertIn("pid", str(ctx.exception))

    def test_stale_lock_is_stolen(self):
        """A lock whose pid is dead is stolen (not honored forever)."""
        from shrinkray.engine.lock import _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("999999999")  # pid that does not exist
        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertEqual(acquired, lock)
        # the stolen lock is re-stamped with OUR pid
        self.assertEqual(int(lock.read_text().split()[0]), os.getpid())
        lock.unlink()

    def test_garbage_lock_content_is_stolen(self):
        from shrinkray.engine.lock import _lock_path, acquire_encode_slot

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        lock.write_text("not-a-pid")
        acquired = acquire_encode_slot(self.out)
        self.assertEqual(acquired, lock)
        lock.unlink()

    def test_a_reused_pid_is_not_adopted_as_live(self):
        """A lock whose pid is alive but was written by a DIFFERENT process
        with that pid is stolen (issue #336): the container restarts pids
        from 1 and the server is pid 1, so after a SIGKILLed run the restarted
        app must not find its own pid in the dead run's lock and refuse to
        encode (the old check was kill(pid, 0) alone)."""
        from shrinkray.engine.lock import _lock_path, acquire_encode_slot
        from shrinkray.tools.process import process_start_ticks

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        live_start = process_start_ticks(os.getpid())
        # our own pid (alive) with a start time that CANNOT match: a reused pid
        lock.write_text(f"{os.getpid()} 1" if live_start != 1 else f"{os.getpid()} 999999999")
        acquired = acquire_encode_slot(self.out, holder="test")
        self.assertEqual(acquired, lock)
        lock.unlink()

    def test_a_matching_start_stamp_is_honoured(self):
        """The positive case of the start stamp: a lock written by THIS
        process (pid + start match) is still a busy lock, however old."""
        from shrinkray.engine.lock import EncodeBusyError, _lock_path, acquire_encode_slot
        from shrinkray.tools.process import process_start_ticks

        self.out.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.out)
        start = process_start_ticks(os.getpid())
        lock.write_text(str(os.getpid()) if start is None else f"{os.getpid()} {start}")
        with self.assertRaises(EncodeBusyError):
            acquire_encode_slot(self.out, holder="test")
        lock.unlink()

    def test_encode_movies_forwards_caller_progress(self):
        """The caller's progress callback receives every encode percent event.

        Regression: the loop shadowed its `progress` parameter with its own rich
        bar, so a job's live progress (the SSE channel) fell silent for the
        whole run and the dashboard showed no percent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            events: list[dict] = []

            def fake_encode(i, o, cmd, progress=None, db_file=None):
                if progress is not None:
                    progress({"stage": "encode", "percent": 42.0})
                return encode.EncodeResult(input=i, output=o, success=True)

            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch(
                    "shrinkray.engine.pipeline._encode_with_command", side_effect=fake_encode
                ),
            ):
                engine_pipeline.encode_movies([source], Path(tmp) / "enc", progress=events.append)
            self.assertEqual([e.get("percent") for e in events], [42.0])

    def test_encode_movies_busy_raises(self):
        """encode_movies propagates EncodeBusyError (no clobber, no swallow)."""
        import tempfile

        from shrinkray.engine.lock import EncodeBusyError, guard_encodes

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            with (
                guard_encodes(Path(tmp) / "enc"),
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                self.assertRaises(EncodeBusyError),
            ):
                engine_pipeline.encode_movies([source], Path(tmp) / "enc")

    def test_encode_movies_reencodes_truncated_debris(self):
        """A truncated output (interrupted encode) is removed and re-encoded,
        not skipped forever (regression: 'output exists' trusted existence
        alone, so crash debris blocked the movie from ever being encoded)."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            debris = out_dir / "movie.mkv"
            debris.write_bytes(b"partial garbage")  # non-empty but truncated
            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=False),
                mock.patch("shrinkray.engine.pipeline._encode_with_command") as enc,
            ):
                enc.side_effect = lambda i, o, cmd, progress=None, db_file=None: (
                    encode.EncodeResult(input=i, output=o, success=True)
                )
                results = engine_pipeline.encode_movies([source], out_dir)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].success)
            # debris was removed BEFORE the encode (no clobber risk)
            self.assertEqual(enc.call_count, 1)

    def test_encode_movies_keeps_verified_output(self):
        """An existing, duration-verifying output is still skipped (the
        normal resume path must keep working)."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            (out_dir / "movie.mkv").write_bytes(b"real output")
            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch("shrinkray.engine.pipeline._encode_with_command") as enc,
            ):
                results = engine_pipeline.encode_movies([source], out_dir)
            self.assertEqual(results, [])
            enc.assert_not_called()

    def test_encode_movies_empty_output_is_reencoded(self):
        """A 0-byte output (very early crash) is re-encoded."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            (out_dir / "movie.mkv").write_bytes(b"")
            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch("shrinkray.engine.pipeline._encode_with_command") as enc,
            ):
                enc.side_effect = lambda i, o, cmd, progress=None, db_file=None: (
                    encode.EncodeResult(input=i, output=o, success=True)
                )
                results = engine_pipeline.encode_movies([source], out_dir)
            self.assertEqual(len(results), 1)

    def test_encode_movies_passes_db_file_for_stall_telemetry(self):
        """The db path given to encode_movies reaches every
        _encode_with_command call (fixed-mode jobs and CLI encode get the
        stall watchdog's failure bundles, as quality mode already does)."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x")
            out_dir = Path(tmp) / "enc"
            out_dir.mkdir()
            db_path = Path(tmp) / "test.db"
            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
                mock.patch("shrinkray.engine.pipeline._encode_with_command") as enc,
            ):
                enc.side_effect = lambda i, o, cmd, progress=None, db_file=None: (
                    encode.EncodeResult(input=i, output=o, success=True)
                )
                engine_pipeline.encode_movies([source], out_dir, db_file=db_path)
            self.assertEqual(enc.call_count, 1)
            self.assertEqual(enc.call_args.kwargs.get("db_file"), db_path)

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

    def test_encode_movies_raises_on_full_disk(self):
        """encode_movies refuses to start when the drive cannot hold the inputs."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mkv"
            source.write_bytes(b"x" * 100)
            with (
                mock.patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                mock.patch(
                    "shrinkray.engine.pipeline.check_disk_space",
                    return_value=(False, "no space left"),
                ),
                self.assertRaises(RuntimeError) as ctx,
            ):
                engine_pipeline.encode_movies([source], Path(tmp) / "enc")
        self.assertIn("no space left", str(ctx.exception))


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
