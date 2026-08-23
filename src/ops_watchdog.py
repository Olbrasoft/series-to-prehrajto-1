#!/usr/bin/env python3
"""Keep upload and preparation workflows running based on repo state."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_status import main as build_status  # noqa: E402
from upload_queue_status import upload_ready_rows  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "reports" / "ops-status.json"

RUNNING = {"queued", "in_progress", "waiting", "pending", "requested"}
FAILED_RETRY_AFTER = dt.timedelta(hours=24)


def run_gh(args: list[str], *, dry_run: bool) -> None:
    print("+ gh " + " ".join(args), flush=True)
    if dry_run:
        return
    subprocess.run(["gh", *args], check=True)


def workflow_has_active_run(workflow: str) -> bool:
    return workflow_active_run_count(workflow) > 0


def workflow_active_run_count(workflow: str) -> int:
    try:
        out = subprocess.check_output(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                f"{workflow}.yml",
                "--limit",
                "20",
                "--json",
                "status",
            ],
            text=True,
        )
        rows = json.loads(out)
    except Exception:
        return 0
    return sum(1 for row in rows if row.get("status") in RUNNING)


def active_workflows(report: dict) -> set[str]:
    active: set[str] = set()
    for row in report.get("workflow_runs") or []:
        if row.get("status") in RUNNING:
            active.add(str(row.get("workflowName")))
    return active


def load_report() -> dict:
    if not REPORT.exists():
        return {}
    return json.loads(REPORT.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def prepared_episode_ids() -> set[int]:
    return {
        int(row["episode_id"])
        for row in load_jsonl(REPO / "plans" / "prepared-episodes.jsonl")
        if row.get("episode_id") is not None
        and row.get("upload_ready")
        and (row.get("selected_source") or {}).get("source_id") is not None
    }


def unprepared_source_queue_episodes() -> int:
    latest = {
        int(row["episode_id"]): row
        for row in load_jsonl(REPO / "plans" / "prepared-episodes.jsonl")
        if row.get("episode_id") is not None
    }
    queue_episode_ids = {
        int(row["episode_id"])
        for path in (
            REPO / "backlog" / "language-audit-queue.jsonl.gz",
            REPO / "backlog" / "series-episodes.jsonl.gz",
        )
        for row in load_jsonl(path)
        if row.get("episode_id") is not None
    }
    now = dt.datetime.now(dt.timezone.utc)
    pending = 0
    for episode_id in queue_episode_ids:
        row = latest.get(episode_id)
        if not row:
            pending += 1
            continue
        if row.get("upload_ready") and (row.get("selected_source") or {}).get("source_id") is not None:
            continue
        prepared_at = row.get("prepared_at")
        if not prepared_at:
            pending += 1
            continue
        try:
            prepared = dt.datetime.fromisoformat(str(prepared_at).replace("Z", "+00:00"))
        except ValueError:
            pending += 1
            continue
        if now - prepared >= FAILED_RETRY_AFTER:
            pending += 1
    return pending


def queue_workflow(
    workflow: str,
    fields: dict[str, str],
    *,
    active: set[str],
    dry_run: bool,
    allow_active: bool = False,
    max_active: int | None = None,
) -> bool:
    active_count = workflow_active_run_count(workflow)
    if max_active is not None and active_count >= max_active:
        print(f"{workflow}: active count {active_count} >= {max_active}")
        return False
    if not allow_active and (workflow in active or active_count > 0):
        print(f"{workflow}: already active")
        return False
    args = ["workflow", "run", f"{workflow}.yml"]
    for key, value in fields.items():
        args.extend(["-f", f"{key}={value}"])
    run_gh(args, dry_run=dry_run)
    active.add(workflow)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-upload-ready", type=int, default=1000)
    ap.add_argument("--target-episodes", type=int, default=3000)
    ap.add_argument("--emergency-episodes", type=int, default=20)
    ap.add_argument("--small-ready-target", type=int, default=1200)
    ap.add_argument("--target-series", type=int, default=240)
    ap.add_argument("--target-backlog-series", type=int, default=10000)
    ap.add_argument("--target-backlog-episodes", type=int, default=1000000)
    ap.add_argument("--target-prepared-episodes", type=int, default=10000)
    ap.add_argument("--min-language-queue-sources", type=int, default=1000)
    ap.add_argument("--prepare-sources-batch", type=int, default=1500)
    ap.add_argument("--min-pending-whisper", type=int, default=100)
    ap.add_argument("--min-whisper-review", type=int, default=1)
    ap.add_argument("--min-description-gap", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    build_status()
    report = load_report()
    counts = report.get("counts") or {}
    gaps = report.get("gaps") or {}
    active = active_workflows(report)
    upload_ready = len(upload_ready_rows())
    backlog_count = int(counts.get("backlog_episodes") or 0)
    manifest_ready = int(counts.get("manifest_upload_ready_episodes") or 0)
    prepared_source_episodes = int(counts.get("prepared_source_episodes") or 0)
    language_queue_sources = int(counts.get("language_queue_sources") or 0)
    unprepared_queue_episodes = unprepared_source_queue_episodes()
    pending_whisper = int(counts.get("language_pending_whisper_sources") or 0)
    whisper_review_pending = int(counts.get("whisper_review_pending_sources") or 0)
    subtitle_followup_pending = int(counts.get("subtitle_followup_pending_sources") or 0)
    description_gap = len(gaps.get("backlog_without_episode_description") or [])
    uploaded_needing_desc_update = len(gaps.get("uploaded_not_marked_description_updated") or [])

    print(
        json.dumps(
            {
                "active_workflows": sorted(active),
                "upload_ready_remaining": upload_ready,
                "backlog_episodes": backlog_count,
                "manifest_upload_ready_episodes": manifest_ready,
                "prepared_source_episodes": prepared_source_episodes,
                "target_prepared_episodes": args.target_prepared_episodes,
                "target_backlog_episodes": args.target_backlog_episodes,
                "language_queue_sources": language_queue_sources,
                "unprepared_source_queue_episodes": unprepared_queue_episodes,
                "language_pending_whisper_sources": pending_whisper,
                "whisper_review_pending_sources": whisper_review_pending,
                "subtitle_followup_pending_sources": subtitle_followup_pending,
                "description_gap_sample": description_gap,
                "uploaded_needing_description_update": uploaded_needing_desc_update,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    has_preparation_work = unprepared_queue_episodes > 0
    prepare_episode_target = min(args.emergency_episodes, 40)
    prepare_series_target = min(args.target_series, 80)

    if has_preparation_work:
        queue_workflow(
            "prepare-manifest",
            {
                "series_limit": str(prepare_series_target),
                "episode_limit": str(prepare_episode_target),
                "source_limit_per_episode": "4",
                "use_whisper": "false",
                "skip_export": "true",
            },
            active=active,
            dry_run=args.dry_run,
            allow_active=False,
            max_active=1,
        )

    if prepared_source_episodes < args.target_prepared_episodes:
        missing_prepared = args.target_prepared_episodes - prepared_source_episodes
        queued_refresh = False
        if (
            backlog_count < args.target_backlog_episodes
            or language_queue_sources < args.min_language_queue_sources
            or unprepared_queue_episodes < min(args.prepare_sources_batch, missing_prepared)
        ):
            queued_refresh = queue_workflow(
                "refresh-backlog",
                {
                    "series_limit": str(args.target_backlog_series),
                    "episode_limit": str(args.target_backlog_episodes),
                    "source_limit_per_episode": "8",
                },
                active=active,
                dry_run=args.dry_run,
            )
        if not queued_refresh:
            queue_workflow(
                "prepare-sources",
                {
                    "episode_limit": str(min(args.prepare_sources_batch, missing_prepared, 100)),
                    "source_limit_per_episode": "8",
                    "use_whisper": "true",
                },
                active=active,
                dry_run=args.dry_run,
            )

    if upload_ready > 0:
        queue_workflow(
            "sync",
            {
                "batch_size": "8",
                "num_shards": "4",
                "allow_subtitles": "true",
                "download_timeout_seconds": "240",
                "max_episode_attempts": "16",
                "continue_uploads": "true",
            },
            active=active,
            dry_run=args.dry_run,
        )

    if pending_whisper >= args.min_pending_whisper:
        queue_workflow(
            "audit-language",
            {"limit": "40", "sample_seconds": "45"},
            active=active,
            dry_run=args.dry_run,
        )

    if whisper_review_pending >= args.min_whisper_review:
        queue_workflow(
            "process-whisper-review",
            {"limit": "20", "sample_seconds": "30"},
            active=active,
            dry_run=args.dry_run,
        )

    if description_gap >= args.min_description_gap or manifest_ready < args.target_episodes:
        queue_workflow(
            "prepare-descriptions",
            {"series_limit": str(args.target_series), "episode_limit": str(args.target_episodes)},
            active=active,
            dry_run=args.dry_run,
        )

    if uploaded_needing_desc_update:
        queue_workflow(
            "update-descriptions",
            {"limit": "100"},
            active=active,
            dry_run=args.dry_run,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
