import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backfill_uploaded_subtitles import (  # noqa: E402
    TERMINAL_STATUSES,
    normalize_srt,
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
