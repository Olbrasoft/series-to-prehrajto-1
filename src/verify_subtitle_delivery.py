#!/usr/bin/env python3
"""Verify a fresh public player page and download its actual Czech subtitle cues."""

import argparse
import hashlib
import json
from pathlib import Path

import requests

from backfill_uploaded_subtitles import detect_subtitle_format, normalize_srt, now_iso, pick_czech_track, vtt_to_srt
from resolve_stream import GOOGLEBOT_HEADERS, parse_html


def verify(url: str, video_id: int, expected_srt: bytes | None = None) -> dict:
    page = requests.get(url, headers={**GOOGLEBOT_HEADERS, "Cache-Control": "no-cache"}, timeout=30)
    page.raise_for_status()
    resolved = parse_html(page.text, url)
    if resolved.video_id != video_id:
        raise ValueError("The public page does not match the expected target video ID")
    track = pick_czech_track(resolved)
    if not track:
        raise ValueError("The target player does not advertise a Czech subtitle track")
    response = requests.get(track, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}, timeout=30)
    response.raise_for_status()
    ext, _ = detect_subtitle_format(response.content)
    if ext not in {".vtt", ".srt"}:
        raise ValueError("The subtitle response is not VTT or SRT")
    normalized = vtt_to_srt(response.content) if ext == ".vtt" else normalize_srt(response.content)
    if expected_srt is not None and normalized != normalize_srt(expected_srt):
        raise ValueError("Delivered subtitle cues differ from the prepared file")
    # Exclude signed CDN URLs, cookies and video stream URLs from the proof.
    return {"checked_at": now_iso(), "detail_url": url, "video_id": video_id,
            "html_http_status": page.status_code, "subtitle_http_status": response.status_code,
            "language": "cs", "format": ext.lstrip("."), "cue_count": normalized.count(b" --> "),
            "subtitle_bytes": len(response.content), "srt_sha256": hashlib.sha256(normalized).hexdigest(),
            "matches_prepared_file": expected_srt is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--video-id", type=int, required=True)
    parser.add_argument("--expected-srt", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        proof = verify(args.url, args.video_id, args.expected_srt.read_bytes() if args.expected_srt else None)
    except Exception as exc:
        print(f"Server verification failed: {type(exc).__name__}")
        return 1
    content = json.dumps(proof, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(content)
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
