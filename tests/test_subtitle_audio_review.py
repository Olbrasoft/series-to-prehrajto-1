"""Protect subtitle identity, parsing, and safe manual-review output."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from review_subtitles_with_whisper import apply_manual_review, identity_matches, parse_cues, render_review, sample_start


class SubtitleAudioReviewTests(unittest.TestCase):
    def parse(self, text, expected_hash=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.srt"
            content = text.encode("utf-8")
            path.write_bytes(content)
            return parse_cues(path, expected_hash or hashlib.sha256(content).hexdigest())

    def test_crlf_milliseconds_and_markup(self):
        cues = self.parse('1\r\n00:01:02,123 --> 00:01:03,456\r\n<i>Hello</i>\r\nworld\r\n\r\n')
        self.assertEqual(cues, [{"start": 62.123, "end": 63.456, "text": "Hello world"}])

    def test_changed_subtitle_cannot_reuse_evidence(self):
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.parse('1\n00:00:01,000 --> 00:00:02,000\nHello\n', "0" * 64)

    def test_invalid_minutes_or_backward_interval(self):
        for interval in ('00:60:01,000 --> 00:60:02,000', '00:00:02,000 --> 00:00:01,000'):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.parse('1\n' + interval + '\nHello\n')

    def test_evidence_is_bound_to_target_track_and_duration(self):
        candidate = {"target_video_id": 123, "file_id": 456, "subtitle_sha256": "hash", "video_duration_sec": 600}
        evidence = {"target_video_id": 123, "subtitle_file_id": 456, "subtitle_sha256": "hash", "target_duration_sec": 600}
        self.assertTrue(identity_matches(evidence, candidate))
        for key in evidence:
            changed = {**evidence, key: "other" if key == "subtitle_sha256" else 999}
            self.assertFalse(identity_matches(changed, candidate))

    def test_short_video_sample_stays_inside_target(self):
        cues = [{"start": 28, "end": 34, "text": "Several words of speech"}]
        self.assertEqual(sample_start(cues, 35, .8, 30), 5)

    def test_review_escapes_external_text(self):
        page = render_review([{"episode_id": 1, "series_title": '<script>alert(1)</script>', "clips": []}])
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('Neither duration nor successful transcription approves attachment', page)

    def test_manual_review_cannot_approve_different_target(self):
        entry = {"target_video_id": 1, "subtitle_file_id": 2, "subtitle_sha256": "hash", "target_duration_sec": 600}
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            apply_manual_review(entry, {**entry, "target_video_id": 3})

    def test_manual_acceptance_requires_distributed_anchors(self):
        entry = {"target_video_id": 1, "subtitle_file_id": 2, "subtitle_sha256": "hash", "target_duration_sec": 600,
                 "clips": [{"fraction": f, "status": "transcribed", "segments": [{"text": "Speech"}]} for f in (.2, .5, .8)]}
        review = {**entry, "reviewer": "tester", "checked_at": "2026-10-12", "status": "sampled_content_and_timing_verified",
                  "semantic_content_confirmed": True, "timing_confirmed": True, "timing_adjustments_applied": False,
                  "reviewed_anchors": [{"fraction": .2, "speech_start_sec": 100.1, "subtitle_start_sec": 100, "speech_minus_subtitle_start_sec": .1}]}
        with self.assertRaisesRegex(ValueError, "three positions"):
            apply_manual_review(entry, review)
        review["reviewed_anchors"] += [{"fraction": .5, "speech_start_sec": 300.2, "subtitle_start_sec": 300, "speech_minus_subtitle_start_sec": .2},
                                     {"fraction": .8, "speech_start_sec": 500.3, "subtitle_start_sec": 500, "speech_minus_subtitle_start_sec": .3}]
        self.assertFalse(apply_manual_review(entry, review)["automatic_attachment_allowed"])
        for field, value in [("semantic_content_confirmed", False), ("timing_confirmed", False), ("timing_adjustments_applied", True)]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "without adjustments"):
                apply_manual_review(entry, {**review, field: value})
        too_late = [{**anchor, "speech_start_sec": anchor["subtitle_start_sec"] + 1.2,
                     "speech_minus_subtitle_start_sec": 1.2} for anchor in review["reviewed_anchors"]]
        with self.assertRaisesRegex(ValueError, "timed anchors"):
            apply_manual_review(entry, {**review, "reviewed_anchors": too_late})

    def test_timing_correction_is_not_an_accepted_verdict(self):
        entry = {"target_video_id": 1, "subtitle_file_id": 2, "subtitle_sha256": "hash", "target_duration_sec": 600}
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            apply_manual_review(entry, {**entry, "status": "requires_constant_offset_review"})


if __name__ == "__main__":
    unittest.main()
