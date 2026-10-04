"""Report actionable original checks, alternate handoffs and verification work."""

import argparse
import json
from pathlib import Path

import backfill_uploaded_subtitles as b
from subtitle_source_cache import SubtitleCache


def queue_status(followup_file, state_files, report_file, *, cache=None, account=None):
    reports = b.load_latest_status(report_file)
    result = {"fast_due": 0, "alternate_pending": 0, "verification_due": 0, "prepared_ready": 0}
    for row, upload in b.pending_uploads(followup_file, state_files, report_file, upload_account=account):
        previous = b.matching_upload_status(reports.get(int(row["episode_id"])), upload)
        if previous.get("status") in b.SUBMITTED_STATUSES:
            result["verification_due"] += int(b.retry_due(previous, verification=True))
            continue
        result["alternate_pending"] += int(b.alternate_route(previous, upload))
        result["prepared_ready"] += int(cache is not None and cache.available(upload))
        result["fast_due"] += int(b.fast_eligible(row, upload, previous, cache))
    result["actionable"] = result["fast_due"] + result["verification_due"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/subtitles"))
    parser.add_argument("--account", choices=["both", "primary", "serialy"], default="both")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = queue_status(b.REPO / "plans/subtitle-followup-queue.jsonl",
                          sorted((b.REPO / "state").glob("uploaded-shard-*.json")),
                          b.REPO / "reports/subtitle-backfill-status.jsonl",
                          cache=SubtitleCache(args.cache_dir), account=None if args.account == "both" else args.account)
    print(json.dumps(result))
    if args.github_output:
        with args.github_output.open("a") as fh:
            for key, value in result.items():
                fh.write(f"{key}={value}\n")
