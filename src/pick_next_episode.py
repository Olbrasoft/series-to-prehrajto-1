#!/usr/bin/env python3
"""Pick the next not-yet-uploaded series episode from backlog."""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKLOG = REPO_ROOT / "backlog" / "series-episodes.jsonl.gz"
DEFAULT_MANIFEST = REPO_ROOT / "manifests" / "upload-ready.jsonl.gz"


def has_rows(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return any(line.strip() for line in fh)


def default_backlog_path() -> Path:
    env_path = os.environ.get("UPLOAD_BACKLOG_PATH")
    if env_path:
        return Path(env_path)
    if has_rows(DEFAULT_MANIFEST):
        return DEFAULT_MANIFEST
    return DEFAULT_BACKLOG


BACKLOG = default_backlog_path()

NUM_SHARDS = int(os.environ.get("SYNC_NUM_SHARDS", "1"))
SHARD_ID = int(os.environ.get("SYNC_SHARD_ID", "0"))
TRANSIENT_FAILURE_COOLDOWN_SECONDS = int(os.environ.get("SYNC_TRANSIENT_FAILURE_COOLDOWN_SECONDS", "21600"))


def state_path(shard_id: int | None = None) -> Path:
    sid = shard_id if shard_id is not None else SHARD_ID
    if NUM_SHARDS <= 1:
        return REPO_ROOT / "state" / "uploaded.json"
    return REPO_ROOT / "state" / f"uploaded-shard-{sid}.json"


STATE = state_path()


def load_backlog(path: Path = BACKLOG) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _merge_states(paths: list[Path]) -> dict:
    merged = {"schema_version": 1, "uploads": [], "failed_attempts": []}
    upload_keys: set[tuple[int, int, int] | tuple[str, int]] = set()
    failures_by_key: dict[tuple[int, int, str], dict] = {}
    failure_order: list[tuple[int, int, str]] = []
    last_updated = None
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        last_updated = max(last_updated or "", state.get("last_updated") or "") or last_updated
        for upload in state.get("uploads", []):
            if upload.get("series_id") is not None and upload.get("season") is not None and upload.get("episode") is not None:
                key: tuple[int, int, int] | tuple[str, int] = episode_key(upload)
            else:
                key = ("episode_id", int(upload["episode_id"]))
            if key in upload_keys:
                continue
            upload_keys.add(key)
            merged["uploads"].append(upload)
        for failure in state.get("failed_attempts", []):
            key = (
                int(failure.get("episode_id") or 0),
                int(failure.get("source_id") or 0),
                str(failure.get("reason") or ""),
            )
            if key not in failures_by_key:
                failure_order.append(key)
            if (failure.get("failed_at") or "") > (failures_by_key.get(key, {}).get("failed_at") or ""):
                failures_by_key[key] = failure
    merged["failed_attempts"] = [failures_by_key[key] for key in failure_order]
    if last_updated:
        merged["last_updated"] = last_updated
    return merged


def load_state(path: Path | None = None) -> dict:
    if path is None:
        paths = [REPO_ROOT / "state" / "uploaded.json"]
        paths.extend(sorted((REPO_ROOT / "state").glob("uploaded-shard-*.json")))
        merged = _merge_states(paths)
        if merged["uploads"] or merged["failed_attempts"]:
            return merged
    p = path or state_path()
    if not p.exists():
        return {"schema_version": 1, "uploads": [], "failed_attempts": []}
    return json.loads(p.read_text(encoding="utf-8"))


def uploaded_episode_ids(state: dict) -> set[int]:
    return {int(u["episode_id"]) for u in state.get("uploads", [])}


def episode_key(row: dict) -> tuple[int, int, int]:
    return (int(row["series_id"]), int(row["season"]), int(row["episode"]))


def uploaded_episode_keys(state: dict) -> set[tuple[int, int, int]]:
    return {episode_key(u) for u in state.get("uploads", [])}


def burned_source_ids(state: dict) -> set[int]:
    return {int(a["source_id"]) for a in state.get("failed_attempts", []) if a.get("permanent")}


def _parse_failed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def cooling_down_source_ids(state: dict, *, now: datetime | None = None) -> set[int]:
    if TRANSIENT_FAILURE_COOLDOWN_SECONDS <= 0:
        return set()
    current = now or datetime.now(timezone.utc)
    cooling: set[int] = set()
    for attempt in state.get("failed_attempts", []):
        if attempt.get("permanent"):
            continue
        failed_at = _parse_failed_at(attempt.get("failed_at"))
        if not failed_at:
            continue
        age = (current - failed_at).total_seconds()
        if 0 <= age < TRANSIENT_FAILURE_COOLDOWN_SECONDS:
            cooling.add(int(attempt["source_id"]))
    return cooling


def pick_next(state: dict, rows: list[dict], extra_exclude: set[int] | None = None) -> dict | None:
    done = uploaded_episode_ids(state)
    done_keys = uploaded_episode_keys(state)
    unavailable = burned_source_ids(state) | cooling_down_source_ids(state)
    extras = extra_exclude or set()
    for item in rows:
        episode_id = int(item["episode_id"])
        if NUM_SHARDS > 1 and episode_id % NUM_SHARDS != SHARD_ID:
            continue
        if episode_id in done or episode_id in extras:
            continue
        if episode_key(item) in done_keys:
            continue
        candidates = [c for c in item["candidates"] if int(c["source_id"]) not in unavailable]
        if not candidates:
            continue
        return {**item, "candidates": candidates}
    return None


def main() -> int:
    if not BACKLOG.is_file():
        print(f"ERROR: backlog missing: {BACKLOG}", file=sys.stderr)
        return 2
    state = load_state()
    rows = load_backlog()
    pick = pick_next(state, rows)
    print(f"state uploads={len(state.get('uploads', []))} failed={len(state.get('failed_attempts', []))}")
    print(f"backlog episodes={len(rows)}")
    if not pick:
        print("backlog exhausted")
        return 1
    print(f"episode_id={pick['episode_id']}")
    print(f"display_name={pick['display_name']!r}")
    print(f"candidates={len(pick['candidates'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
