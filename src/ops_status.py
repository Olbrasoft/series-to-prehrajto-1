#!/usr/bin/env python3
"""Summarize operational status for uploads, descriptions and source prep."""

from __future__ import annotations

import gzip
import json
import subprocess
from collections import Counter
from pathlib import Path

from description_quality import is_valid_generated_description
from pick_next_episode import load_state

REPO = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def gh_runs() -> list[dict]:
    try:
        out = subprocess.check_output(
            [
                "gh", "run", "list",
                "--repo", "Olbrasoft/series-to-prehrajto",
                "--limit", "20",
                "--json", "databaseId,workflowName,status,conclusion,createdAt,headBranch",
            ],
            text=True,
        )
        return json.loads(out)
    except Exception:
        return []


def latest_descriptions(rows: list[dict]) -> tuple[set[int], set[int]]:
    series: set[int] = set()
    episodes: set[int] = set()
    for row in rows:
        if row.get("status") != "ok":
            continue
        if not is_valid_generated_description(row.get("generated_description") or ""):
            continue
        if row.get("kind") == "series":
            series.add(int(row["series_id"]))
        elif row.get("kind") == "episode":
            episodes.add(int(row["episode_id"]))
    return series, episodes


def unresolved_description_errors(rows: list[dict]) -> int:
    ok_keys: set[tuple[str, int]] = set()
    err_keys: set[tuple[str, int]] = set()
    for row in rows:
        kind = row.get("kind")
        entity_id = row.get("episode_id") if kind == "episode" else row.get("series_id")
        if not kind or not entity_id:
            continue
        key = (kind, int(entity_id))
        if row.get("status") == "ok":
            if is_valid_generated_description(row.get("generated_description") or ""):
                ok_keys.add(key)
        elif row.get("status") == "error":
            err_keys.add(key)
    return len(err_keys - ok_keys)


def source_ids(rows: list[dict]) -> set[int]:
    ids: set[int] = set()
    for row in rows:
        try:
            ids.add(int(row["source_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def whisper_status(row: dict) -> str:
    return ((row.get("signals") or {}).get("whisper") or {}).get("status") or "missing"


def row_status(row: dict) -> str:
    return str(row.get("status") or "missing")


def main() -> int:
    backlog = load_jsonl(REPO / "backlog" / "series-episodes.jsonl.gz")
    manifest = load_jsonl(REPO / "manifests" / "upload-ready.jsonl.gz")
    source_queue = load_jsonl(REPO / "backlog" / "language-audit-queue.jsonl.gz")
    state = load_state()
    descriptions = load_jsonl(REPO / "plans" / "descriptions.jsonl")
    prepared = load_jsonl(REPO / "plans" / "prepared-episodes.jsonl")
    audits = load_jsonl(REPO / "audits" / "language-audit.jsonl")
    latest_audits = load_jsonl(REPO / "audits" / "language-audit-latest.jsonl.gz")
    subtitle_followup = load_jsonl(REPO / "plans" / "subtitle-followup-queue.jsonl")
    whisper_review = load_jsonl(REPO / "plans" / "whisper-review-queue.jsonl")
    desc_series, desc_episodes = latest_descriptions(descriptions)
    uploaded = state.get("uploads", [])
    uploaded_episode_ids = {int(row["episode_id"]) for row in uploaded}
    backlog_episode_ids = {int(row["episode_id"]) for row in backlog}
    prepared_episode_ids = {int(row["episode_id"]) for row in prepared}
    queue_source_ids = source_ids(source_queue)
    audited_source_ids = source_ids(latest_audits or audits)
    whisper_attempted_source_ids = {
        int(row["source_id"])
        for row in latest_audits
        if row.get("source_id") is not None and whisper_status(row) not in {"disabled", "missing"}
    }
    uploaded_missing_desc = [
        row for row in uploaded
        if int(row["episode_id"]) not in desc_episodes and int(row["series_id"]) not in desc_series
    ]
    not_ready = [row for row in prepared if not row.get("upload_ready")]
    pending_subtitles = [
        row
        for row in subtitle_followup
        if row_status(row) not in {"done", "completed", "subtitle_attached", "closed"}
        and str(row.get("subtitle_status") or "") != "done"
    ]
    pending_whisper_review = [
        row
        for row in whisper_review
        if row_status(row) in {"missing", "needs_whisper", "whisper_failed"}
    ]
    report = {
        "counts": {
            "backlog_episodes": len(backlog_episode_ids),
            "manifest_upload_ready_episodes": len({int(row["episode_id"]) for row in manifest}),
            "uploaded_episodes": len(uploaded_episode_ids),
            "prepared_source_episodes": len(prepared_episode_ids),
            "description_series": len(desc_series),
            "description_episodes": len(desc_episodes),
            "description_errors": unresolved_description_errors(descriptions),
            "language_audit_rows": len(audits),
            "language_queue_sources": len(queue_source_ids),
            "language_audited_sources": len(audited_source_ids),
            "language_whisper_attempted_sources": len(whisper_attempted_source_ids),
            "language_pending_whisper_sources": len(queue_source_ids - whisper_attempted_source_ids),
            "whisper_review_sources": len(whisper_review),
            "whisper_review_pending_sources": len(pending_whisper_review),
            "subtitle_followup_sources": len(subtitle_followup),
            "subtitle_followup_pending_sources": len(pending_subtitles),
        },
        "workflow_runs": gh_runs(),
        "gaps": {
            "backlog_without_source_plan": sorted(backlog_episode_ids - prepared_episode_ids)[:50],
            "backlog_without_episode_description": sorted(backlog_episode_ids - desc_episodes)[:50],
            "uploaded_without_gemma_description": [
                {"episode_id": row["episode_id"], "display_name": row["display_name"], "video_id": row["prehrajto_video_id"]}
                for row in uploaded_missing_desc
            ],
            "uploaded_not_marked_description_updated": [
                {"episode_id": row["episode_id"], "display_name": row["display_name"], "video_id": row["prehrajto_video_id"]}
                for row in uploaded if not row.get("description_updated_at")
            ],
            "prepared_not_upload_ready": [
                {"episode_id": row["episode_id"], "series_title": row["series_title"], "season": row["season"], "episode": row["episode"]}
                for row in not_ready[:50]
            ],
            "whisper_review_pending": [
                {
                    "episode_id": row.get("episode_id"),
                    "source_id": row.get("source_id"),
                    "series_title": row.get("series_title"),
                    "episode_code": row.get("episode_code"),
                    "source_title": row.get("source_title"),
                    "status": row_status(row),
                }
                for row in pending_whisper_review[:50]
            ],
            "subtitle_followup_pending": [
                {
                    "episode_id": row.get("episode_id"),
                    "source_id": row.get("source_id"),
                    "series_title": row.get("series_title"),
                    "episode_code": row.get("episode_code"),
                    "source_title": row.get("source_title"),
                    "subtitle_status": row.get("subtitle_status"),
                    "reason": row.get("reason"),
                }
                for row in pending_subtitles[:50]
            ],
        },
        "language_verdicts": dict(Counter(row.get("verdict", "UNKNOWN") for row in audits)),
        "language_whisper_statuses": dict(Counter(whisper_status(row) for row in latest_audits)),
        "whisper_review_statuses": dict(Counter(row_status(row) for row in whisper_review)),
        "subtitle_followup_statuses": dict(Counter(row.get("subtitle_status") or row_status(row) for row in subtitle_followup)),
    }
    out = REPO / "reports" / "ops-status.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    print(f"uploaded_without_gemma_description={len(uploaded_missing_desc)}")
    print(f"backlog_without_source_plan={len(backlog_episode_ids - prepared_episode_ids)}")
    print(f"backlog_without_episode_description={len(backlog_episode_ids - desc_episodes)}")
    print(f"whisper_review_pending_sources={len(pending_whisper_review)}")
    print(f"subtitle_followup_pending_sources={len(pending_subtitles)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
