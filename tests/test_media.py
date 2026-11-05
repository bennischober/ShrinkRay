"""Media probing tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import unittest

from shrinkray.tools import ffprobe as read_media


class TestReadMedia(unittest.TestCase):
    def test_parse_frame_rate(self):
        self.assertEqual(read_media.parse_frame_rate("25/1"), 25.0)
        self.assertAlmostEqual(read_media.parse_frame_rate("30000/1001"), 29.97002997, places=6)
        self.assertEqual(read_media.parse_frame_rate("29.97"), 29.97)
        self.assertIsNone(read_media.parse_frame_rate("0/0"))
        self.assertIsNone(read_media.parse_frame_rate(None))
        self.assertIsNone(read_media.parse_frame_rate("garbage"))

    def test_get_raw_duration(self):
        self.assertEqual(
            read_media.get_raw_duration(
                {"tags": {"NUMBER_OF_FRAMES-eng": 100}, "avg_frame_rate": "25/1"}
            ),
            4.0,
        )
        self.assertEqual(
            read_media.get_raw_duration({"nb_frames": 100, "avg_frame_rate": "30000/1001"}),
            100 / (30000 / 1001),
        )
        self.assertIsNone(read_media.get_raw_duration({}))
        self.assertIsNone(
            read_media.get_raw_duration({"tags": {"NUMBER_OF_FRAMES": 10}, "avg_frame_rate": "0/0"})
        )

    def test_get_duration(self):
        self.assertEqual(
            read_media.get_duration({"tags": {"DURATION-eng": "01:02:03.400000000"}}), "01:02:03"
        )
        self.assertEqual(
            read_media.get_duration({"tags": {"DURATION-de": "01:02:03.400000000"}}), "01:02:03"
        )
        self.assertEqual(
            read_media.get_duration({"tags": {"DURATION": "01:02:03.400000000"}}), "01:02:03"
        )
        self.assertIsNone(read_media.get_duration({"tags": {}}))
        self.assertIsNone(read_media.get_duration({}))
        # configured language takes precedence
        self.assertEqual(
            read_media.get_duration(
                {"tags": {"DURATION-de": "02:00:00", "DURATION-eng": "01:00:00"}}, "de"
            ),
            "02:00:00",
        )
        self.assertEqual(
            read_media.get_duration(
                {"tags": {"DURATION-de": "02:00:00", "DURATION-eng": "01:00:00"}}, "en"
            ),
            "01:00:00",
        )

    def test_get_video_stream(self):
        streams = [{"codec_type": "audio"}, {"codec_type": "video", "codec_name": "h264"}]
        self.assertEqual(read_media.get_video_stream(streams)["codec_name"], "h264")
        self.assertIsNone(read_media.get_video_stream([]))
        self.assertIsNone(read_media.get_video_stream([{"codec_type": "audio"}]))

    def test_get_audio_languages(self):
        streams = [
            {"codec_type": "audio", "tags": {"language": "de"}},
            {"codec_type": "audio", "tags": {"language": "en"}},
            {"codec_type": "audio", "tags": {}},
            {"codec_type": "video", "tags": {"language": "de"}},
        ]
        self.assertEqual(read_media.get_audio_languages(streams), "de, en")
        self.assertEqual(read_media.get_audio_languages([]), "")

    def test_get_aspect_ratio(self):
        self.assertEqual(read_media.get_aspect_ratio(1920, 1080), "16x9")
        self.assertEqual(read_media.get_aspect_ratio(3840, 2160), "16x9")

    def test_is_hdr(self):
        """Issue #518: the detection is code-fixed on the transfer set - no
        config reaches it; a bt2020nc SDR stream (an SDR transfer) is not HDR."""
        hdr_stream = {"color_space": "bt2020nc", "color_transfer": "smpte2084"}
        sdr_stream = {"color_space": "bt709", "color_transfer": "bt709"}
        self.assertTrue(read_media.is_hdr(hdr_stream))
        self.assertFalse(read_media.is_hdr(sdr_stream))
        self.assertFalse(read_media.is_hdr({}))


if __name__ == "__main__":
    unittest.main()
