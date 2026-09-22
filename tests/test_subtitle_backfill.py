import sys
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backfill_uploaded_subtitles import (  # noqa: E402
    TERMINAL_STATUSES,
    normalize_srt,
    build_tasks,
    pick_czech_track,
    vtt_to_srt,
)
from resolve_stream import parse_html  # noqa: E402


def multilingual_source_html() -> str:
    return """
    <script>
      videos.push({ src: "https://cdn.example/video.mp4", type: 'video/mp4', res: '720', label: '720p' });
      var tracks = [
        { file: "https://cdn.example/eng.vtt?token=1", label: "CS - 100 - eng", kind: "captions" },
        { file: "https://cdn.example/cze.vtt?token=1", label: "CS - 101 - cze", kind: "captions" }
      ];
      var normalizedTracks = [
        { src: "https://cdn.example/eng.vtt?token=1", srclang: "cs", label: "CS - 100 - eng" },
        { src: "https://cdn.example/cze.vtt?token=1", srclang: "cs", label: "CS - 101 - cze" }
      ];
    </script>
    """


def test_track_label_overrides_generic_czech_srclang_and_deduplicates_urls():
    resolved = parse_html(multilingual_source_html(), "https://prehraj.to/example/token")

    assert [(track.lang, track.label) for track in resolved.tracks] == [
        ("eng", "CS - 100 - eng"),
        ("cze", "CS - 101 - cze"),
    ]
    assert pick_czech_track(resolved) == "https://cdn.example/cze.vtt?token=1"


def test_backfill_never_falls_back_to_an_explicitly_foreign_track():
    html = multilingual_source_html().replace("CS - 101 - cze", "CS - 101 - ger")
    resolved = parse_html(html, "https://prehraj.to/example/token")

    assert pick_czech_track(resolved) is None


def test_generated_czech_upload_filename_is_recognized_from_label():
    html = """
    <script>
      videos.push({ src: "https://cdn.example/video.mp4", type: 'video/mp4', res: '720', label: '720p' });
      var tracks = [
        { file: "https://cdn.example/generated.vtt?token=1", label: "CS - 12838243 - cs-1790089830-1" }
      ];
    </script>
    """
    resolved = parse_html(html, "https://prehraj.to/example/token")

    assert resolved.tracks[0].lang == "cs"
    assert pick_czech_track(resolved) == "https://cdn.example/generated.vtt?token=1"


def test_vtt_is_converted_to_strict_crlf_srt():
    converted = vtt_to_srt(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500 align:start\nAhoj\n\n"
        b"00:00:03.000 --> 00:00:04.000\nSvet\n"
    )

    assert converted == (
        b"1\r\n00:00:01,000 --> 00:00:02,500\r\nAhoj\r\n\r\n"
        b"2\r\n00:00:03,000 --> 00:00:04,000\r\nSvet\r\n"
    )
    assert b"\n" not in converted.replace(b"\r\n", b"")


def test_existing_srt_is_normalized_and_renumbered():
    converted = normalize_srt(
        b"9\n00:00:01.000 --> 00:00:02.500\nAhoj\n\n"
        b"10\n00:00:03,000 --> 00:00:04,000\nSvet\n"
    )

    assert converted.startswith(b"1\r\n00:00:01,000 --> 00:00:02,500")
    assert b"\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000" in converted


def test_transient_target_lookup_failures_are_retried_later():
    assert "target_not_found" not in TERMINAL_STATUSES
    assert "target_processing" not in TERMINAL_STATUSES
    assert "target_unresolved" not in TERMINAL_STATUSES
    assert "source_track_not_found" in TERMINAL_STATUSES
    assert "uploaded" in TERMINAL_STATUSES


@pytest.mark.parametrize("max_rows,limit", [(1, 10), (10, 1)])
def test_bounded_backfill_prioritizes_unseen_uploads_and_rotates_retries(
    tmp_path, monkeypatch, max_rows, limit
):
    followups = tmp_path / "followups.jsonl"
    state = tmp_path / "state.json"
    report = tmp_path / "report.jsonl"
    followups.write_text("".join(json.dumps({"episode_id": i}) + "\n" for i in range(1, 6)))
    state.write_text(json.dumps({"uploads": [
        {"episode_id": i, "prehrajto_video_id": i, "display_name": "Example CZ Titulky",
         "uploaded_at": f"2026-09-{i + 10:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]}))
    report.write_text("".join(json.dumps(row) + "\n" for row in [
        {"episode_id": 1, "status": "target_not_found", "checked_at": "2026-09-21T10:00:00Z"},
        {"episode_id": 4, "status": "target_not_found", "checked_at": "2026-09-22T10:00:00Z"},
        {"episode_id": 5, "status": "uploaded", "checked_at": "2026-09-22T10:00:00Z"},
    ]))
    args = Namespace(followup_file=followups, state_file=[state], report_file=report,
                     episode_id=[], retry_reported=False, max_rows=max_rows,
                     limit=limit, lookup="public", search_min_interval=0)
    inspected = []

    def lookup(upload, **kwargs):
        inspected.append(upload["episode_id"])
        return {"processing": False, "detail_url": "https://example.test/video",
                "resolved": SimpleNamespace(tracks=[])}

    monkeypatch.setattr("backfill_uploaded_subtitles.find_uploaded_detail", lookup)
    # Newest unseen first, older unseen next, then least recently checked retry.
    for expected in [3, 2, 1, 4]:
        tasks = build_tasks(args, None)
        assert [task[0]["episode_id"] for task in tasks] == [expected]
        with report.open("a") as fh:
            fh.write(json.dumps({"episode_id": expected, "status": "target_not_found",
                                 "checked_at": "2026-09-23T10:00:00Z"}) + "\n")
    assert inspected == [3, 2, 1, 4]
