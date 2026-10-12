#!/usr/bin/env python3
"""Prepare read-only Whisper evidence for manual subtitle timing review.

The input is a JSON array of duration-qualified candidates. Each row needs
episode_id, target_video_id, target_detail_url, video_duration_sec, file_id,
subtitle_sha256 and local_subtitle_file. This tool never attaches subtitles.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import subprocess
import wave
from collections import Counter
from pathlib import Path

import requests

from resolve_stream import GOOGLEBOT_HEADERS, ResolveError, parse_html, resolve

FRACTIONS = (0.2, 0.5, 0.8)
TIME_RE = re.compile(
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})[^\n]*\n"
    r"(.*?)(?=\n\s*\n|\Z)", re.S,
)


def read_player(url):
    """Prefer the direct public page; use the configured paced proxy if needed."""
    try:
        response = requests.get(url, headers={**GOOGLEBOT_HEADERS, "Cache-Control": "no-cache"}, timeout=25)
        response.raise_for_status()
        return parse_html(response.text, url)
    except (requests.RequestException, ResolveError):
        if not os.environ.get("CZ_PROXY_URL"):
            raise
        with contextlib.redirect_stdout(io.StringIO()):
            return resolve(url, max_retries=1)


def parse_cues(path: Path, expected_sha256: str) -> list[dict]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("Subtitle hash mismatch")
    encodings = ("utf-16",) if content.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1250")
    for encoding in encodings:
        try:
            text = content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Unsupported subtitle encoding")

    def seconds(parts):
        h, m, s, ms = map(int, parts)
        if m >= 60 or s >= 60:
            raise ValueError("Invalid subtitle timestamp")
        return h * 3600 + m * 60 + s + ms / 1000

    cues = []
    for match in TIME_RE.finditer(text):
        start, end = seconds(match.groups()[:4]), seconds(match.groups()[4:8])
        if end <= start:
            raise ValueError("Invalid subtitle interval")
        cues.append({"start": start, "end": end,
                     "text": html.unescape(re.sub(r"<[^>]+>", "", match.group(9))).replace("\n", " ").strip()})
    if not cues:
        raise ValueError("No timed subtitle cues")
    return cues


def sample_start(cues: list[dict], duration: float, fraction: float, seconds: float) -> float:
    center = duration * fraction
    starts = [max(0, min(duration - seconds, cue["start"] - 3))
              for cue in cues if abs(cue["start"] - center) <= 45]
    if not starts:
        starts = [max(0, min(duration - seconds, center - seconds / 2))]

    def score(start):
        active = [cue for cue in cues if start + 2 <= cue["start"] and cue["end"] <= start + seconds - 2]
        return sum(len(cue["text"].split()) for cue in active) - 0.05 * abs(start + seconds / 2 - center)

    return round(max(starts, key=score), 3)


def identity_matches(entry: dict, candidate: dict) -> bool:
    return (int(entry.get("target_video_id") or 0) == int(candidate["target_video_id"])
            and int(entry.get("subtitle_file_id") or 0) == int(candidate["file_id"])
            and entry.get("subtitle_sha256") == candidate["subtitle_sha256"]
            and entry.get("target_duration_sec") == candidate["video_duration_sec"])


def save_json(path: Path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def apply_manual_review(entry: dict, review: dict) -> dict:
    """Attach an explicitly recorded assessment to exactly the reviewed files."""
    for key in ("target_video_id", "subtitle_file_id", "subtitle_sha256", "target_duration_sec"):
        if review.get(key) != entry.get(key):
            raise ValueError("Manual review identity mismatch")
    status = review.get("status")
    if status not in {"sampled_content_and_timing_verified", "timing_mismatch", "inconclusive"}:
        raise ValueError("Unsupported manual review verdict")
    if not review.get("reviewer") or not review.get("checked_at"):
        raise ValueError("Manual reviewer and review time are required")
    clips = entry.get("clips", [])
    if (len(clips) != 3 or {c.get("fraction") for c in clips} != set(FRACTIONS)
            or any(c.get("status") != "transcribed" or not c.get("segments") for c in clips)):
        raise ValueError("Manual review requires complete transcription evidence")
    if status == "sampled_content_and_timing_verified":
        if (review.get("semantic_content_confirmed") is not True
                or review.get("timing_confirmed") is not True
                or review.get("timing_adjustments_applied") is not False):
            raise ValueError("Manual acceptance requires content and timing confirmation without adjustments")
        anchors = review.get("reviewed_anchors", [])
        if ({a["fraction"] for a in anchors} != set(FRACTIONS)
                or any(abs(a["speech_start_sec"] - a["subtitle_start_sec"]) > 1
                       or abs((a["speech_start_sec"] - a["subtitle_start_sec"])
                              - a["speech_minus_subtitle_start_sec"]) > .01 for a in anchors)):
            raise ValueError("Manual acceptance requires timed anchors at all three positions")
    return {**entry, "status": status, "manual_review": review, "automatic_attachment_allowed": False}


def render_review(entries: list[dict]) -> str:
    blocks = ["<!doctype html><meta charset='utf-8'><title>Subtitle audio review</title>",
              "<style>body{font:16px sans-serif;max-width:1400px;margin:24px auto}table{width:100%;border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc;vertical-align:top;width:50%}pre{white-space:pre-wrap}</style>",
              "<h1>Subtitle audio review</h1><p>Transcription is evidence for manual review. Neither duration nor successful transcription approves attachment.</p><p>Accept only manually confirmed content and timing in all three samples. Reject uncertain or offset tracks; do not shift or retime subtitles.</p>"]
    for entry in entries:
        title = f"{entry.get('series_title', '')} {entry.get('episode_code', '')} — {entry['episode_id']}"
        blocks.append(f"<h2>{html.escape(title)}</h2><p>{html.escape(entry.get('status', ''))}</p>")
        for clip in entry.get("clips", []):
            blocks.append(f"<h3>Position {clip['fraction']:.0%}, start {clip['start_sec']:.3f}s</h3>")
            language = html.escape(str(clip.get("detected_language", "unknown")))
            blocks.append(f"<p>Detected language: {language}. Clip status: {html.escape(clip.get('status', ''))}</p>")
            audio_path = clip.get("local_audio")
            if audio_path:
                blocks.append(f"<audio controls src='{html.escape(Path(audio_path).as_uri(), quote=True)}'></audio>")
            transcript = "\n".join(f"{s['start']:.3f}–{s['end']:.3f}: {s['text']}" for s in clip.get("segments", []))
            subtitles = "\n".join(f"{c['start']:.3f}–{c['end']:.3f}: {c['text']}" for c in clip.get("subtitle_cues", []))
            blocks.append(f"<table><tr><th>Whisper speech, absolute video time</th><th>Czech subtitle cues</th></tr><tr><td><pre>{html.escape(transcript)}</pre></td><td><pre>{html.escape(subtitles)}</pre></td></tr></table>")
    return "\n".join(blocks)


def collect(candidate: dict, model, args, out: Path) -> dict:
    cues = parse_cues(Path(candidate["local_subtitle_file"]), candidate["subtitle_sha256"])
    result = {k: candidate[k] for k in ("episode_id", "target_video_id", "target_detail_url")}
    result.update(series_title=candidate.get("series_title"), episode_code=candidate.get("episode_code"),
                  subtitle_file_id=candidate["file_id"], subtitle_sha256=candidate["subtitle_sha256"],
                  checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), model=args.model,
                  device=args.device, task="translate", clips=[], automatic_attachment_allowed=False)
    try:
        video = read_player(candidate["target_detail_url"])
        if video.video_id != int(candidate["target_video_id"]):
            raise ValueError("Target identity mismatch")
        if video.duration_sec != candidate["video_duration_sec"]:
            raise ValueError("Target duration changed")
        if not video.videos or video.duration_sec < args.clip_seconds:
            raise ValueError("Target audio unavailable")
        gap = video.duration_sec - max(cue["end"] for cue in cues)
        if not -3 <= gap <= 60:
            raise ValueError("Duration preliminary filter failed")
        stream = min(video.videos, key=lambda v: v.res or 9999)
        result.update(target_duration_sec=video.duration_sec, stream_resolution=stream.res)
        for fraction in FRACTIONS:
            start = sample_start(cues, video.duration_sec, fraction, args.clip_seconds)
            clip = {"fraction": fraction, "start_sec": start, "sample_duration_sec": args.clip_seconds,
                    "subtitle_cues": [cue for cue in cues if cue["end"] > start and cue["start"] < start + args.clip_seconds]}
            result["clips"].append(clip)
            audio = out / "audio" / f"{candidate['target_video_id']}-{candidate['file_id']}-{int(fraction * 100)}.wav"
            try:
                command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                           "-rw_timeout", "20000000", "-user_agent", GOOGLEBOT_HEADERS["User-Agent"],
                           "-ss", str(start), "-i", stream.url, "-t", str(args.clip_seconds),
                           "-vn", "-ac", "1", "-ar", "16000", str(audio)]
                process = subprocess.run(command, capture_output=True, timeout=args.extract_timeout)
                if process.returncode or not audio.exists():
                    clip.update(status="audio_extract_failed", returncode=process.returncode)
                    continue
                audio.chmod(0o600)
                with wave.open(str(audio), "rb") as pcm:
                    pcm_digest = hashlib.sha256(pcm.readframes(pcm.getnframes())).hexdigest()
                segments, info = model.transcribe(str(audio), task="translate", beam_size=5,
                                                  vad_filter=True, word_timestamps=True,
                                                  condition_on_previous_text=False)
                clip.update(status="transcribed", detected_language=info.language,
                            language_probability=info.language_probability,
                            local_audio=str(audio), audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
                            audio_pcm_sha256=pcm_digest,
                            segments=[{"start": round(start + s.start, 3), "end": round(start + s.end, 3),
                                       "text": s.text.strip(), "avg_logprob": s.avg_logprob,
                                       "words": [{"start": round(start + w.start, 3), "end": round(start + w.end, 3),
                                                  "word": w.word, "probability": w.probability} for w in (s.words or [])]}
                                      for s in segments])
            except Exception as error:
                # Exception text and FFmpeg stderr may contain signed stream URLs.
                clip.update(status="audio_or_transcription_failed", error_type=type(error).__name__)
            print(f"episode={candidate['episode_id']} position={fraction} status={clip['status']}", flush=True)
        result["status"] = ("awaiting_manual_review" if all(c["status"] == "transcribed" and c.get("segments") for c in result["clips"])
                            else "incomplete_evidence")
    except Exception as error:
        result.update(status="target_check_failed", error_type=type(error).__name__)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--episode-id", type=int, action="append")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--clip-seconds", type=int, default=30)
    parser.add_argument("--extract-timeout", type=int, default=120)
    parser.add_argument("--import-evidence", type=Path, help="Reuse identity-bound existing Whisper JSON evidence.")
    parser.add_argument("--manual-review", type=Path, help="Apply identity-bound manual verdicts; never enable attachment.")
    parser.add_argument("--render-only", action="store_true", help="Render imported evidence without network or model calls.")
    args = parser.parse_args()
    if args.clip_seconds < 10 or args.extract_timeout <= 0:
        parser.error("Use clips of at least ten seconds and a positive extraction timeout")
    os.umask(0o077)
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    (out / "audio").mkdir(exist_ok=True)
    candidates = json.loads(args.candidates.read_text())
    if args.episode_id:
        candidates = [row for row in candidates if int(row["episode_id"]) in set(args.episode_id)]
    if not candidates:
        parser.error("No matching candidates")
    evidence_path = out / "whisper-evidence.json"
    imported = json.loads(args.import_evidence.read_text()) if args.import_evidence else []
    previous = json.loads(evidence_path.read_text()) if evidence_path.exists() else []
    entries = {int(row["episode_id"]): row for row in previous + imported}
    reviews = {int(row["episode_id"]): row for row in json.loads(args.manual_review.read_text())} if args.manual_review else {}
    model = None
    for candidate in candidates:
        episode_id = int(candidate["episode_id"])
        parse_cues(Path(candidate["local_subtitle_file"]), candidate["subtitle_sha256"])
        cached = entries.get(episode_id)
        usable_cache = (cached is not None and identity_matches(cached, candidate)
                        and {c.get("fraction") for c in cached.get("clips", [])} == set(FRACTIONS)
                        and all(c.get("status") == "transcribed" and c.get("segments") for c in cached["clips"]))
        if usable_cache:
            cached["status"] = "awaiting_manual_review"
            cached["automatic_attachment_allowed"] = False
        elif args.render_only:
            parser.error(f"Missing matching complete evidence for episode {episode_id}")
        else:
            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type, cpu_threads=6)
            entries[episode_id] = collect(candidate, model, args, out)
        if episode_id in reviews:
            entries[episode_id] = apply_manual_review(entries[episode_id], reviews[episode_id])
        save_json(evidence_path, list(entries.values()))
    ordered = [entries[int(candidate["episode_id"])] for candidate in candidates]
    page = out / "manual-review.html"
    page.write_text(render_review(ordered))
    page.chmod(0o600)
    save_json(out / "summary.json", {"candidate_count": len(ordered),
                                    "status_counts": dict(Counter(entry["status"] for entry in ordered)),
                                    "automatic_attachment_allowed": False})
    print(f"prepared={len(ordered)} review={page}")


if __name__ == "__main__":
    main()
