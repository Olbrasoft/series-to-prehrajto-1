#!/usr/bin/env python3
"""Audit source language signals and persist import-ready evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from language_checks import CZECH_AUDIO_LANGS, title_language_hint, whisper_language  # noqa: E402
from resolve_stream import ResolveError, pick_best, resolve as resolve_prehrajto  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

CZ_DUB_RE = re.compile(
    r"(?:\bcz[\s\-_]*dab(?:ing)?\b|\bczdab\w*|\bczdub\w*|\bcesk[aáyý][\s\-_]*dab(?:ing)?\b|\bc[zs][\s\-_]*dabing\b|cesky[\s\-_]*dabing|cz[\s\-_]*\.dab\b)",
    re.IGNORECASE,
)
CZ_SUB_RE = re.compile(
    r"(?:\bcz[\s\-_]*tit(?:ulky)?\b|\bcztit\w*|\bcz[\s\-_]*subs?\b|\bc[zs][\s\-_]*titulky\b|cesk[yé][\s\-_]*titulky)",
    re.IGNORECASE,
)
SK_DUB_RE = re.compile(r"(?:\bsk[\s\-_]*dab(?:ing)?\b|\bskdab\w*|\bskdub\w*)", re.IGNORECASE)
SK_SUB_RE = re.compile(r"(?:\bsk[\s\-_]*tit(?:ulky)?\b|\bsktit\w*)", re.IGNORECASE)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def iter_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def whisper_status(row: dict) -> str | None:
    return ((row.get("signals") or {}).get("whisper") or {}).get("status")


def completed_source_ids(path: Path, *, require_whisper_attempt: bool) -> set[int]:
    done: set[int] = set()
    for row in iter_jsonl(path):
        try:
            source_id = int(row["source_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if require_whisper_attempt and whisper_status(row) in {None, "disabled"}:
            continue
        done.add(source_id)
    return done


def write_latest_index(audit_path: Path, latest_path: Path) -> None:
    latest: dict[int, dict] = {}
    for row in iter_jsonl(audit_path):
        try:
            source_id = int(row["source_id"])
        except (KeyError, TypeError, ValueError):
            continue
        old = latest.get(source_id)
        if old is None or str(row.get("audited_at") or "") >= str(old.get("audited_at") or ""):
            latest[source_id] = row
    rows = sorted(
        latest.values(),
        key=lambda item: (
            str(item.get("series_slug") or ""),
            int(item.get("season") or 0),
            int(item.get("episode") or 0),
            int(item.get("source_id") or 0),
        ),
    )
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if latest_path.suffix == ".gz" else open
    with opener(latest_path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def title_lang_class(title: str | None) -> str:
    if not title:
        return "UNKNOWN"
    if CZ_DUB_RE.search(title):
        return "CZ_DUB"
    if SK_DUB_RE.search(title):
        return "SK_DUB"
    if CZ_SUB_RE.search(title):
        return "CZ_SUB"
    if SK_SUB_RE.search(title):
        return "SK_SUB"
    return "UNKNOWN"


def metadata_audio_lang(item: dict) -> tuple[str | None, float | None, str | None]:
    audio = (item.get("db_audio_lang") or "").strip().lower()
    if audio:
        return audio, item.get("db_audio_confidence"), item.get("db_audio_detected_by") or "db"
    metadata = item.get("metadata") or {}
    for key in ("audio_lang", "language", "lang"):
        value = (metadata.get(key) or "").strip().lower() if isinstance(metadata.get(key), str) else ""
        if value:
            return value, None, f"metadata.{key}"
    return None, None, None


def verdict_from_signals(title_class: str, audio_lang: str | None, whisper_lang: str | None) -> tuple[str, str, float]:
    if whisper_lang:
        if whisper_lang in {"cs", "cz", "ces", "cze"}:
            return "CZ_AUDIO", "whisper", 0.95
        if whisper_lang == "sk":
            return "SK_AUDIO", "whisper", 0.95
        return "NOT_CZ_AUDIO", "whisper", 0.95
    if audio_lang:
        if audio_lang in CZECH_AUDIO_LANGS:
            return "CZ_AUDIO", "metadata", 0.85
        if audio_lang == "sk":
            return "SK_AUDIO", "metadata", 0.85
        return "NOT_CZ_AUDIO", "metadata", 0.80
    if title_class in {"CZ_DUB", "CZ_NATIVE"}:
        return "PROBABLE_CZ_AUDIO", "title", 0.55
    if title_class == "CZ_SUB":
        return "CZ_SUBTITLES_ONLY", "title", 0.50
    return "UNKNOWN", "none", 0.0


def verification_status(verdict: str, detected_by: str, audio_lang: str | None, whisper: dict, title_class: str) -> str:
    whisper_lang = whisper.get("language") if whisper.get("status") == "ok" else None
    if whisper_lang in {"cs", "cz", "ces", "cze"}:
        return "whisper_confirmed_cz_audio"
    if whisper_lang and whisper_lang not in {"cs", "cz", "ces", "cze"}:
        if audio_lang in CZECH_AUDIO_LANGS or title_class.startswith("CZ"):
            return "whisper_contradicts_cz_signal"
        return "whisper_confirmed_non_cz_audio"
    if audio_lang in CZECH_AUDIO_LANGS:
        return "metadata_cz_audio_whisper_unconfirmed"
    if detected_by == "provider_tracks" and verdict == "CZ_SUBTITLES_ONLY":
        return "provider_confirmed_cz_subtitles"
    if title_class.startswith("CZ"):
        return "title_cz_signal_unverified"
    return "unverified"


def fetch_sktorrent_page(url: str) -> dict:
    import requests

    resp = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"},
        stream=True,
    )
    raw = resp.raw.read(500_000, decode_content=False)
    text = raw.decode("utf-8", "replace")
    if resp.status_code >= 400:
        return {"status": "http_error", "http_status": resp.status_code}
    streams = []
    for match in re.finditer(r"https?://[^\"']+/media/videos//h264/[^\"']+?_(\d+)p\.mp4", text):
        streams.append({"url": match.group(0), "label": f"{match.group(1)}p", "res": int(match.group(1))})
    tracks = []
    for match in re.finditer(r"https?://[^\"']+/media/videos/vtt/\d+/([^/\"']+)\.vtt", text):
        tracks.append({"url": match.group(0), "lang": match.group(1).lower()})
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', text)
    title = title_match.group(1) if title_match else None
    return {
        "status": "ok",
        "title": title,
        "streams": sorted(streams, key=lambda item: -item["res"]),
        "tracks": tracks,
    }


def pick_stream(streams: list[dict]) -> dict | None:
    if not streams:
        return None
    by_res = {int(s["res"]): s for s in streams}
    for res in (1080, 720, 480):
        if res in by_res:
            return by_res[res]
    return streams[0]


def sample_audio(stream_url: str, out_path: Path, *, start_sec: int, seconds: int) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start_sec),
        "-t",
        str(seconds),
        "-headers",
        "Referer: https://prehraj.to/\r\n",
        "-i",
        stream_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return False, proc.stderr.strip()[:300]
    return True, "ok"


def audit_provider(item: dict, *, sample_seconds: int, probe_stream: bool = False) -> dict:
    if item.get("provider") == "sktorrent" and item.get("source_url"):
        probe = fetch_sktorrent_page(item["source_url"])
        if probe.get("status") != "ok":
            return {"provider": "sktorrent", **probe}
        if os.environ.get("WHISPER_LANGUAGE_CHECK") != "1":
            return {"provider": "sktorrent", **probe, "whisper": {"status": "disabled"}}
        stream = pick_stream(probe.get("streams") or [])
        if not stream:
            return {"provider": "sktorrent", **probe, "whisper": {"status": "no_stream"}}
        with tempfile.TemporaryDirectory() as td:
            sample = Path(td) / "sample.wav"
            ok, msg = sample_audio(stream["url"], sample, start_sec=60, seconds=sample_seconds)
            if not ok:
                return {"provider": "sktorrent", **probe, "whisper": {"status": "sample_failed", "error": msg, "resolution": stream["label"]}}
            lang, prob, status = whisper_language(sample, seconds=sample_seconds)
            return {"provider": "sktorrent", **probe, "whisper": {"status": status, "language": lang, "probability": prob, "resolution": stream["label"]}}

    if os.environ.get("WHISPER_LANGUAGE_CHECK") != "1" and not probe_stream:
        return {"provider": item.get("provider"), "whisper": {"status": "disabled"}}
    if item.get("provider") != "prehrajto" or not item.get("source_url"):
        return {"provider": item.get("provider"), "whisper": {"status": "unsupported_provider"}}
    try:
        resolved = resolve_prehrajto(item["source_url"], max_retries=1, backoff_seconds=(3,))
        best = pick_best(resolved.videos, prefer=(1080, 720))
    except ResolveError as exc:
        return {"provider": "prehrajto", "whisper": {"status": "resolve_failed", "error": str(exc), "permanent": exc.permanent}}
    except Exception as exc:
        return {"provider": "prehrajto", "whisper": {"status": "resolve_crashed", "error": f"{type(exc).__name__}: {exc}"}}
    probe = {
        "provider": "prehrajto",
        "status": "ok",
        "video_id": resolved.video_id,
        "title": resolved.name,
        "duration_sec": resolved.duration_sec,
        "streams": [
            {"label": video.label, "res": video.res, "default": video.is_default}
            for video in resolved.videos
        ],
        "tracks": [
            {"lang": track.lang}
            for track in resolved.tracks
        ],
    }
    if os.environ.get("WHISPER_LANGUAGE_CHECK") != "1":
        return {**probe, "whisper": {"status": "disabled"}}
    with tempfile.TemporaryDirectory() as td:
        sample = Path(td) / "sample.wav"
        ok, msg = sample_audio(best.url, sample, start_sec=60, seconds=sample_seconds)
        if not ok:
            return {**probe, "whisper": {"status": "sample_failed", "error": msg, "resolution": best.label}}
        lang, prob, status = whisper_language(sample, seconds=sample_seconds)
        return {**probe, "whisper": {"status": status, "language": lang, "probability": prob, "resolution": best.label}}


def audit_one(item: dict, *, use_whisper: bool, sample_seconds: int, probe_stream: bool = False) -> dict:
    provider_probe = audit_provider(item, sample_seconds=sample_seconds, probe_stream=probe_stream)
    provider_title = provider_probe.get("title")
    source_title = item.get("source_title") or provider_title or ""
    title_class = title_lang_class(source_title)
    title_hint = title_language_hint(source_title)
    audio_lang, audio_conf, audio_by = metadata_audio_lang(item)
    whisper = provider_probe.get("whisper") or {"status": "disabled"}
    old = os.environ.get("WHISPER_LANGUAGE_CHECK")
    if use_whisper:
        os.environ["WHISPER_LANGUAGE_CHECK"] = "1"
        provider_probe = audit_provider(item, sample_seconds=sample_seconds, probe_stream=True)
        whisper = provider_probe.get("whisper") or {"status": "disabled"}
    if old is None:
        os.environ.pop("WHISPER_LANGUAGE_CHECK", None)
    else:
        os.environ["WHISPER_LANGUAGE_CHECK"] = old

    whisper_lang = whisper.get("language") if whisper.get("status") == "ok" else None
    verdict, detected_by, confidence = verdict_from_signals(title_class, audio_lang, whisper_lang)
    if verdict == "UNKNOWN" and title_hint == "cz_audio_title":
        verdict, detected_by, confidence = "PROBABLE_CZ_AUDIO", "title", 0.55
    track_langs = {track.get("lang") for track in provider_probe.get("tracks", []) if track.get("lang")}
    if verdict == "UNKNOWN" and track_langs & {"cze", "cz", "cs", "ces", "cesky", "česky"}:
        verdict, detected_by, confidence = "CZ_SUBTITLES_ONLY", "provider_tracks", 0.70
    verification = verification_status(verdict, detected_by, audio_lang, whisper, title_class)
    mismatch = bool(title_class.startswith("CZ") and verdict == "NOT_CZ_AUDIO")
    if item.get("db_lang_class") in {"CZ_DUB", "CZ_NATIVE"} and verdict == "NOT_CZ_AUDIO":
        mismatch = True
    unresolved = verdict == "UNKNOWN" and (
        item.get("db_lang_class") in {None, "UNKNOWN"}
        or not item.get("db_audio_lang")
        or not item.get("source_title")
        or whisper.get("status") in {"unsupported_provider", "resolve_failed", "sample_failed"}
    )

    return {
        "audited_at": now_iso(),
        "series_id": item["series_id"],
        "series_slug": item["series_slug"],
        "series_title": item["series_title"],
        "episode_id": item["episode_id"],
        "season": item["season"],
        "episode": item["episode"],
        "episode_name": item.get("episode_name") or item.get("episode_title"),
        "provider": item["provider"],
        "source_id": item["source_id"],
        "external_id": item.get("external_id"),
        "source_url": item.get("source_url"),
        "source_title": item.get("source_title"),
        "duration_sec": item.get("duration_sec"),
        "resolution_hint": item.get("resolution_hint"),
        "filesize_bytes": item.get("filesize_bytes"),
        "view_count": item.get("view_count"),
        "source_origin": item.get("source_origin"),
        "db_source_exists": item.get("db_source_exists"),
        "quality_tier": item.get("quality_tier"),
        "provider_title": provider_title,
        "db_lang_class": item.get("db_lang_class"),
        "db_audio_lang": item.get("db_audio_lang"),
        "signals": {
            "title_lang_class": title_class,
            "title_hint": title_hint,
            "metadata_audio_lang": audio_lang,
            "metadata_audio_confidence": audio_conf,
            "metadata_audio_detected_by": audio_by,
            "provider_probe": provider_probe,
            "whisper": whisper,
        },
        "verdict": verdict,
        "detected_by": detected_by,
        "confidence": confidence,
        "verification_status": verification,
        "cz_audio_verified": verification == "whisper_confirmed_cz_audio",
        "needs_db_update": (
            verdict != "UNKNOWN"
            and detected_by in {"whisper", "metadata", "provider_tracks"}
            and (
                item.get("db_lang_class") in {None, "UNKNOWN"}
                or item.get("db_audio_lang") != ("cs" if verdict in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"} else item.get("db_audio_lang"))
                or mismatch
            )
        ),
        "needs_manual_resolution": unresolved,
        "mismatch": mismatch,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="backlog/language-audit-queue.jsonl.gz")
    ap.add_argument("--out", default="audits/language-audit.jsonl")
    ap.add_argument("--latest-out", default="audits/language-audit-latest.jsonl.gz")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--source-id", type=int, action="append")
    ap.add_argument("--series-slug")
    ap.add_argument("--use-whisper", action="store_true")
    ap.add_argument("--sample-seconds", type=int, default=45)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    rows = load_jsonl(Path(args.queue))
    if args.source_id:
        wanted = set(args.source_id)
        rows = [row for row in rows if int(row["source_id"]) in wanted]
    if args.series_slug:
        rows = [row for row in rows if row["series_slug"] == args.series_slug]
    if not args.refresh:
        done = completed_source_ids(Path(args.out), require_whisper_attempt=args.use_whisper)
        rows = [row for row in rows if int(row["source_id"]) not in done]
    available = len(rows)
    rows = rows[: args.limit]
    results = [audit_one(row, use_whisper=args.use_whisper, sample_seconds=args.sample_seconds) for row in rows]
    append_jsonl(Path(args.out), results)
    write_latest_index(Path(args.out), Path(args.latest_out))
    for result in results:
        whisper = whisper_status(result) or "none"
        print(
            f"{result['source_id']} {result['series_title']} "
            f"S{int(result['season']):02d}E{int(result['episode']):02d} "
            f"{result['verdict']} by={result['detected_by']} whisper={whisper} "
            f"verification={result['verification_status']} "
            f"update={result['needs_db_update']}"
        )
    remaining = max(available - len(results), 0)
    print(f"Appended {len(results)} audit rows to {args.out}")
    print(f"Wrote latest source index to {args.latest_out}")
    if remaining:
        print(f"Remaining queued rows after this batch: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
