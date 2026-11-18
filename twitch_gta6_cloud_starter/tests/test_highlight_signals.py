from unittest import TestCase
from unittest.mock import patch

from app import highlight_signals


class HighlightSignalsTests(TestCase):
    def test_chat_spike_late_in_vod_beats_silence_and_video_scan_reaches_the_end(self):
        chat = [10.0, 20.0, *([720.5] * 16)]
        with patch.object(highlight_signals, "_sample", return_value=b"") as sample:
            start, analysis = highlight_signals.select_window("source.mp4", 900, 30, False, chat)
        self.assertEqual(analysis["event_seconds"], 720)
        self.assertEqual(analysis["chat_peaks"][0]["messages"], 16)
        self.assertEqual(start, 711)
        self.assertEqual(sample.call_args.args[:3], ("source.mp4", 876, 24))

    def test_vision_can_choose_a_ranked_moment(self):
        chat = [10.0, *([120.0] * 10), *([180.0] * 16)]
        selection = {"event_seconds": 120, "reason": "Le joueur réagit."}
        with patch.object(highlight_signals, "_sample", return_value=b""), patch.object(
            highlight_signals, "_vision_choice", return_value=(selection, None)
        ) as vision:
            start, analysis = highlight_signals.select_window(
                "source.mp4", 240, 30, False, chat, vision_enabled=True,
            )
        self.assertEqual(vision.call_args.args[2], [180, 120])
        self.assertEqual(analysis["vision"], selection)
        self.assertEqual(start, 111)
