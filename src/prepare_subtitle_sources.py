#!/usr/bin/env python3
"""Discover and download Czech subtitles independently of account uploads."""

from __future__ import annotations

import argparse
import datetime as dt
import time
from collections import deque
from pathlib import Path

import backfill_uploaded_subtitles as b
from subtitle_source_cache import SubtitleCache, cache_key


def fair_order(pending, cache):
    """Alternate accounts, rotating oldest attempts within each account."""
    groups = {}
    for pair in pending:
        groups.setdefault(pair[1]["upload_account"], []).append(pair)
    for account, rows in groups.items():
        rows.sort(key=lambda pair: cache.entries.get(cache_key(pair[1]), {}).get("checked_at", ""))
        groups[account] = deque(rows)
    accounts = sorted(groups, key=lambda account: account == cache.scheduler.get("last_account"))
    while any(groups.values()):
        for account in accounts:
            if groups[account]:
                yield groups[account].popleft()


def prepare(args) -> int:
    cache = SubtitleCache(args.cache_dir)
    reports = b.load_latest_status(args.report_file)
    pending = b.pending_uploads(args.followup_file, args.state_file, args.report_file)
    # Pending POSTs are verification-only. Discovery never attaches or removes.
    pending = [(row, upload) for row, upload in pending
               if b.matching_upload_status(reports.get(int(row["episode_id"])), upload).get("status") not in b.SUBMITTED_STATUSES]
    cache.prune([upload for _, upload in pending])
    for row, upload in pending:
        previous = b.matching_upload_status(reports.get(int(row["episode_id"])), upload)
        cache.invalidate_duration(upload, previous.get("target_duration"))
    cached_ready = sum(cache.ready(upload) for _, upload in pending)
    fast_due = sum(b.fast_eligible(row, upload, b.matching_upload_status(reports.get(int(row["episode_id"])), upload), cache)
                   for row, upload in pending)
    # During the initial original-source sweep, reserve most request capacity
    # for the two attachment workers. This is deliberately a smaller budget.
    priority = fast_due >= 100 and not getattr(args, "episode_id", None)
    runtime = min(args.max_runtime, 120) if priority else args.max_runtime
    interval = max(args.search_min_interval, 60) if priority else args.search_min_interval
    if priority:
        import resolve_stream
        resolve_stream.RESOLVE_MIN_GAP = max(resolve_stream.RESOLVE_MIN_GAP, 60)
    pending = [(row, upload) for row, upload in pending
               if b.alternate_route(b.matching_upload_status(reports.get(int(row["episode_id"])), upload), upload)
               and not b.original_recheck_due(reports[int(row["episode_id"])])]
    if getattr(args, "episode_id", None):
        pending = [(row, upload) for row, upload in pending if int(row["episode_id"]) in args.episode_id]
    now = dt.datetime.now(dt.timezone.utc)
    pending = list(fair_order(pending, cache))
    deadline = time.monotonic() + runtime
    processed = ready = 0
    b.log(f"alternate queue waiting={len(pending)} fast_due={fast_due} runtime={runtime} search_interval={interval}")
    for row, upload in pending:
        if time.monotonic() >= deadline or processed >= args.limit or cached_ready >= args.max_ready:
            break
        previous = b.matching_upload_status(reports.get(int(row["episode_id"])), upload)
        if previous.get("status") in {"target_not_found", "target_deleted", "target_processing"}:
            continue
        if not previous.get("detail_url") or not cache.due(upload, now):
            continue
        processed += 1
        cache.scheduler["last_account"] = upload["upload_account"]
        entry = cache.entries.get(cache_key(upload), {})
        progress = entry.get("search_progress", {}) if entry.get("original_source_url") == upload.get("source_url") else {}
        duration = previous.get("target_duration") or entry.get("target_duration")
        def checkpoint(value):
            cache.record(upload, "searching", retry_hours=0, search_progress=value, target_duration=duration)
        checkpoint(progress)
        try:
            if not duration:
                target = b.resolve(previous["detail_url"], max_retries=1)
                if b.pick_czech_track(target):
                    cache.record(upload, "target_has_tracks", retry_hours=24)
                    continue
                duration = target.duration_sec
                if not duration:
                    cache.record(upload, "unknown_duration", retry_hours=24)
                    continue
            source_url, track = b.find_alternate_track(
                row, duration, min_interval=interval,
                deadline=min(deadline, time.monotonic() + getattr(args, "episode_runtime", 180)),
                progress=progress, checkpoint=checkpoint)
            if not track:
                cache.record(upload, "no_track", retry_hours=168)
                continue
            content = b.fetch_subtitle(track)
            ext, _ = b.detect_subtitle_format(content)
            if ext not in {".vtt", ".srt"}:
                cache.record(upload, "unsupported_format", retry_hours=168)
                continue
            content = b.vtt_to_srt(content) if ext == ".vtt" else b.normalize_srt(content)
            cache.put(upload, source_url, duration, content)
            ready += 1
            cached_ready += 1
            b.log(f"prepared subtitles episode_id={row['episode_id']} account={upload['upload_account']}")
        except b.SourceDiscoveryDeferred as exc:
            exhausted = str(exc) == "runtime_budget"
            cache.record(upload, "deferred", reason=str(exc), retry_hours=0.25 if exhausted else 6,
                         search_progress=progress, target_duration=duration)
            b.log(f"discovery deferred episode_id={row['episode_id']} reason={exc}")
            if not exhausted or time.monotonic() >= deadline:
                break
        except Exception as exc:
            cache.record(upload, "deferred", reason=type(exc).__name__, retry_hours=6,
                         search_progress=progress, target_duration=duration)
            b.log(f"source preparation deferred episode_id={row['episode_id']} reason={type(exc).__name__}")
    b.log(f"subtitle discovery processed={processed} prepared={ready} cached_ready={cached_ready}")
    if getattr(args, "github_output", None):
        with args.github_output.open("a") as fh:
            fh.write(f"prepared={ready}\nready={cached_ready}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/subtitles"))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-ready", type=int, default=100)
    parser.add_argument("--max-runtime", type=int, default=600)
    parser.add_argument("--episode-runtime", type=int, default=180)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--search-min-interval", type=float, default=30)
    parser.add_argument("--state-file", type=Path, action="append", default=[])
    parser.add_argument("--episode-id", type=int, action="append", default=[])
    parser.add_argument("--followup-file", type=Path, default=b.REPO / "plans/subtitle-followup-queue.jsonl")
    parser.add_argument("--report-file", type=Path, default=b.REPO / "reports/subtitle-backfill-status.jsonl")
    args = parser.parse_args()
    args.state_file = args.state_file or sorted((b.REPO / "state").glob("uploaded-shard-*.json"))
    return prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
