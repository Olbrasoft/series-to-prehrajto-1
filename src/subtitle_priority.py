"""Yield source-search capacity to subtitles while the upload queue is full."""

import argparse
import json
from pathlib import Path

from backfill_uploaded_subtitles import REPO, pending_uploads
from upload_queue_status import upload_ready_rows


def hold_source_search(ready: int, subtitle_pending: int) -> bool:
    # Leave headroom for uploads committed between manifest rebuilds.
    return ready >= 900 and subtitle_pending >= 100


def current_priority() -> dict:
    ready = len(upload_ready_rows(require_description=False))
    pending = len(pending_uploads(REPO / "plans/subtitle-followup-queue.jsonl",
                                 sorted((REPO / "state").glob("uploaded-shard-*.json")),
                                 REPO / "reports/subtitle-backfill-status.jsonl"))
    return {"upload_ready": ready, "subtitle_pending": pending,
            "hold_source_search": hold_source_search(ready, pending)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = current_priority()
    print(json.dumps(result))
    if args.github_output:
        with args.github_output.open("a") as fh:
            fh.write(f"hold_source_search={str(result['hold_source_search']).lower()}\n")
