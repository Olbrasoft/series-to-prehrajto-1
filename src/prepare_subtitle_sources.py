#!/usr/bin/env python3
"""Discover and download Czech subtitles independently of account uploads."""

from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import backfill_uploaded_subtitles as b
from subtitle_source_cache import SubtitleCache, cache_key


def prepare(args) -> int:
    cache = SubtitleCache(args.cache_dir)
    reports = b.load_latest_status(args.report_file)
    pending = b.pending_uploads(args.followup_file, args.state_file, args.report_file)
    # Pending POSTs are verification-only. Discovery never attaches or removes.
    pending = [(row, upload) for row, upload in pending
               if b.matching_upload_status(reports.get(int(row["episode_id"])), upload).get("status") not in b.SUBMITTED_STATUSES]
    cache.prune([upload for _, upload in pending])
    now = dt.datetime.now(dt.timezone.utc)
    pending.sort(key=lambda pair: (
        not pair[0].get("title_match_recheck", False),
        cache.entries.get(cache_key(pair[1]), {}).get("checked_at", ""),
    ))
    deadline = time.monotonic() + args.max_runtime
    processed = ready = 0
    cached_ready = sum(cache.ready(upload) for _, upload in pending)
    for row, upload in pending:
        if time.monotonic() >= deadline or processed >= args.limit or cached_ready >= args.max_ready:
            break
        previous = b.matching_upload_status(reports.get(int(row["episode_id"])), upload)
        if previous.get("status") in {"target_not_found", "target_deleted", "target_processing"}:
            continue
        if not previous.get("detail_url") or not cache.due(upload, now):
            continue
        processed += 1
        try:
            target = b.resolve(previous["detail_url"], max_retries=1)
            if b.pick_czech_track(target):
                cache.record(upload, "target_has_tracks", retry_hours=24)
                continue
            if not target.duration_sec:
                cache.record(upload, "unknown_duration", retry_hours=24)
                continue
            source_url, track = b.source_with_subtitles(row)
            if not track:
                source_url, track = b.find_alternate_track(row, target.duration_sec, min_interval=args.search_min_interval)
            if not track:
                cache.record(upload, "no_track", retry_hours=168)
                continue
            content = b.fetch_subtitle(track)
            ext, _ = b.detect_subtitle_format(content)
            if ext not in {".vtt", ".srt"}:
                cache.record(upload, "unsupported_format", retry_hours=168)
                continue
            content = b.vtt_to_srt(content) if ext == ".vtt" else b.normalize_srt(content)
            cache.put(upload, source_url, target.duration_sec, content)
            ready += 1
            cached_ready += 1
            b.log(f"prepared subtitles episode_id={row['episode_id']} account={upload['upload_account']}")
        except b.SourceDiscoveryDeferred as exc:
            cache.record(upload, "deferred", reason=str(exc), retry_hours=6)
            b.log(f"discovery deferred episode_id={row['episode_id']} reason={exc}")
            # One shared worker obeys the exhausted search budget for both accounts.
            break
        except Exception as exc:
            cache.record(upload, "deferred", reason=type(exc).__name__, retry_hours=6)
            b.log(f"source preparation deferred episode_id={row['episode_id']} reason={type(exc).__name__}")
    b.log(f"subtitle discovery processed={processed} prepared={ready} cached_ready={cached_ready}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/subtitles"))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-ready", type=int, default=100)
    parser.add_argument("--max-runtime", type=int, default=600)
    parser.add_argument("--search-min-interval", type=float, default=30)
    parser.add_argument("--state-file", type=Path, action="append", default=[])
    parser.add_argument("--followup-file", type=Path, default=b.REPO / "plans/subtitle-followup-queue.jsonl")
    parser.add_argument("--report-file", type=Path, default=b.REPO / "reports/subtitle-backfill-status.jsonl")
    args = parser.parse_args()
    args.state_file = args.state_file or sorted((b.REPO / "state").glob("uploaded-shard-*.json"))
    return prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
