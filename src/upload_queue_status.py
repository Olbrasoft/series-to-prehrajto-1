#!/usr/bin/env python3
"""Report upload-ready episodes using the same constraints as sync_batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pick_next_episode import (  # noqa: E402
    SHARD_ID,
    NUM_SHARDS,
    burned_source_ids,
    cooling_down_source_ids,
    load_backlog,
    load_state,
    uploaded_episode_ids,
    uploaded_episode_keys,
)
from sync_batch import load_description_plans, load_source_plans  # noqa: E402


def episode_key(row: dict) -> tuple[int, int, int]:
    return (int(row["series_id"]), int(row["season"]), int(row["episode"]))


def manifest_selected_source_id(row: dict) -> int | None:
    source_plan = ((row.get("upload_manifest") or {}).get("source_plan") or {})
    selected_id = source_plan.get("selected_source_id")
    return int(selected_id) if selected_id is not None else None


def upload_ready_rows(
    *,
    require_description: bool = True,
    require_source_plan: bool = True,
    respect_shard: bool = False,
) -> list[dict]:
    rows = load_backlog()
    state = load_state()
    done_ids = uploaded_episode_ids(state)
    done_keys = uploaded_episode_keys(state)
    unavailable = burned_source_ids(state) | cooling_down_source_ids(state)
    source_plans = load_source_plans()
    descriptions = load_description_plans()
    ready = []
    for row in rows:
        episode_id = int(row["episode_id"])
        if respect_shard and NUM_SHARDS > 1 and episode_id % NUM_SHARDS != SHARD_ID:
            continue
        if episode_id in done_ids or episode_key(row) in done_keys:
            continue
        if require_description:
            upload_job = (row.get("upload_manifest") or {}).get("upload_job") or {}
            manifest_description = ((row.get("upload_manifest") or {}).get("description") or {}).get("text")
            has_description = (
                bool(upload_job.get("description"))
                or bool(manifest_description)
                or episode_id in descriptions["episode"]
                or int(row["series_id"]) in descriptions["series"]
            )
            if not has_description:
                continue
        candidates = row.get("candidates") or []
        live_source_ids = {int(candidate["source_id"]) for candidate in candidates if int(candidate["source_id"]) not in unavailable}
        if not live_source_ids:
            continue
        if require_source_plan:
            upload_job = (row.get("upload_manifest") or {}).get("upload_job") or {}
            if upload_job and not upload_job.get("source_url"):
                continue
            selected_id = manifest_selected_source_id(row)
            if selected_id is not None:
                if selected_id not in live_source_ids:
                    continue
            else:
                plan = source_plans.get(episode_id)
                selected = (plan or {}).get("selected_source") or {}
                selected_id = int(selected["source_id"]) if selected.get("source_id") is not None else None
                if not plan or not plan.get("upload_ready") or not selected_id or selected_id not in live_source_ids:
                    continue
        ready.append(row)
    return ready


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-description", action="store_true", default=True)
    ap.add_argument("--no-require-description", action="store_false", dest="require_description")
    ap.add_argument("--require-source-plan", action="store_true", default=True)
    ap.add_argument("--no-require-source-plan", action="store_false", dest="require_source_plan")
    ap.add_argument("--respect-shard", action="store_true")
    ap.add_argument("--min-ready", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ready = upload_ready_rows(
        require_description=args.require_description,
        require_source_plan=args.require_source_plan,
        respect_shard=args.respect_shard,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "remaining_upload_ready": len(ready),
                    "min_ready": args.min_ready,
                    "threshold_met": len(ready) >= args.min_ready,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"remaining_upload_ready={len(ready)}")
    return 0 if len(ready) >= args.min_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
