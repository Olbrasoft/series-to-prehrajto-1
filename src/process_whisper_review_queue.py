#!/usr/bin/env python3
"""Verify ambiguous episode sources with Whisper and promote Czech audio hits."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_language_sources import audit_one, append_jsonl, write_latest_index  # noqa: E402
from prepare_episode_sources import (  # noqa: E402
    MIN_PLANNED_FILE_SIZE,
    load_jsonl,
    source_quality_tier,
    update_subtitle_followup_queue,
    write_compacted_prepared,
    write_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_quality_resolution(audit: dict) -> int:
    probe = (audit.get("signals") or {}).get("provider_probe") or {}
    streams = probe.get("streams") or []
    if streams:
        return max(int(stream.get("res") or 0) for stream in streams)
    hint = str(audit.get("resolution_hint") or "")
    for value in ("2160", "1440", "1080", "720"):
        if value in hint:
            return int(value)
    return 0


def prepared_row_from_audit(audit: dict, *, upload_kind: str) -> dict:
    needs_subtitles = upload_kind == "subtitles"
    source_verdict = "CZ_SUBTITLES_ONLY" if needs_subtitles else "CZ_AUDIO"
    verification_status = (
        "whisper_confirmed_non_cz_audio_needs_cz_subtitles"
        if needs_subtitles
        else audit.get("verification_status")
    )
    selected = {
        **audit,
        "original_verdict": audit.get("verdict"),
        "verdict": source_verdict,
        "verification_status": verification_status,
        "resolution_score": source_quality_resolution(audit),
        "quality_tier": source_quality_tier(
            {
                "source_title": audit.get("source_title"),
                "resolution_hint": audit.get("resolution_hint"),
                "filesize_bytes": audit.get("filesize_bytes"),
            },
            resolved_resolution=source_quality_resolution(audit),
        ),
    }
    return {
        "prepared_at": audit.get("audited_at") or now_iso(),
        "episode_id": audit["episode_id"],
        "series_id": audit["series_id"],
        "series_slug": audit["series_slug"],
        "series_title": audit["series_title"],
        "season": audit["season"],
        "episode": audit["episode"],
        "imdb_votes": 0,
        "imdb_rating": 0,
        "csfd_rating": 0,
        "episode_name": audit.get("episode_name"),
        "tested_source_count": 1,
        "upload_ready": True,
        "upload_kind": upload_kind,
        "needs_subtitles_after_upload": needs_subtitles,
        "selected_source": selected,
        "tested_sources": [selected],
        "whisper_review_sources": [],
    }


def queue_key(row: dict) -> tuple[int, int]:
    return int(row["episode_id"]), int(row["source_id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="plans/whisper-review-queue.jsonl")
    ap.add_argument("--prepared", default="plans/prepared-episodes.jsonl")
    ap.add_argument("--audit-out", default="audits/language-audit.jsonl")
    ap.add_argument("--audit-latest-out", default="audits/language-audit-latest.jsonl.gz")
    ap.add_argument("--subtitle-followup-out", default="plans/subtitle-followup-queue.jsonl")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--series-slug")
    ap.add_argument("--season", type=int)
    ap.add_argument("--episode", type=int)
    ap.add_argument("--sample-seconds", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    queue_path = REPO_ROOT / args.queue
    rows = load_jsonl(queue_path)
    wanted = []
    for row in rows:
        if not args.refresh and row.get("status") not in {None, "needs_whisper"}:
            continue
        if args.series_slug and row.get("series_slug") != args.series_slug:
            continue
        if args.season is not None and int(row.get("season") or 0) != args.season:
            continue
        if args.episode is not None and int(row.get("episode") or 0) != args.episode:
            continue
        wanted.append(row)
    wanted = wanted[: args.limit]

    old_whisper = os.environ.get("WHISPER_LANGUAGE_CHECK")
    os.environ["WHISPER_LANGUAGE_CHECK"] = "1"
    try:
        audits = [
            audit_one(row, use_whisper=True, sample_seconds=args.sample_seconds, probe_stream=True)
            for row in wanted
        ]
    finally:
        if old_whisper is None:
            os.environ.pop("WHISPER_LANGUAGE_CHECK", None)
        else:
            os.environ["WHISPER_LANGUAGE_CHECK"] = old_whisper

    append_jsonl(REPO_ROOT / args.audit_out, audits)
    write_latest_index(REPO_ROOT / args.audit_out, REPO_ROOT / args.audit_latest_out)

    audit_by_key = {queue_key(audit): audit for audit in audits}
    updated_queue = []
    promoted = []
    promoted_keys: set[tuple[int, int]] = set()
    for row in rows:
        audit = audit_by_key.get(queue_key(row))
        if not audit:
            updated_queue.append(row)
            continue
        whisper = (audit.get("signals") or {}).get("whisper") or {}
        status = "whisper_confirmed_cz" if audit.get("verdict") == "CZ_AUDIO" else "whisper_non_cz_needs_subtitles"
        if whisper.get("status") != "ok":
            status = "whisper_failed"
        updated = {
            **row,
            "checked_at": audit.get("audited_at") or now_iso(),
            "status": status,
            "verdict": audit.get("verdict"),
            "detected_by": audit.get("detected_by"),
            "verification_status": audit.get("verification_status"),
            "whisper": whisper,
        }
        updated_queue.append(updated)
        if (
            status == "whisper_confirmed_cz"
            and int(audit.get("filesize_bytes") or 0) >= MIN_PLANNED_FILE_SIZE
        ):
            promoted.append(prepared_row_from_audit(audit, upload_kind="audio"))
            promoted_keys.add(queue_key(audit))
        elif (
            status == "whisper_non_cz_needs_subtitles"
            and int(audit.get("filesize_bytes") or 0) >= MIN_PLANNED_FILE_SIZE
        ):
            promoted.append(prepared_row_from_audit(audit, upload_kind="subtitles"))
            promoted_keys.add(queue_key(audit))

    write_jsonl(queue_path, updated_queue)
    if promoted:
        write_compacted_prepared(REPO_ROOT / args.prepared, promoted)
        update_subtitle_followup_queue(REPO_ROOT / args.subtitle_followup_out, promoted)

    for audit in audits:
        whisper = (audit.get("signals") or {}).get("whisper") or {}
        print(
            json.dumps(
                {
                    "episode": f"{audit['series_title']} S{int(audit['season']):02d}E{int(audit['episode']):02d}",
                    "source_title": audit.get("source_title"),
                    "verdict": audit.get("verdict"),
                    "detected_by": audit.get("detected_by"),
                    "whisper_status": whisper.get("status"),
                    "whisper_language": whisper.get("language"),
                    "whisper_probability": whisper.get("probability"),
                    "promoted": queue_key(audit) in promoted_keys,
                },
                ensure_ascii=False,
            )
        )
    print(f"Processed {len(audits)} whisper review sources")
    print(f"Promoted {len(promoted)} sources to prepared upload plans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
