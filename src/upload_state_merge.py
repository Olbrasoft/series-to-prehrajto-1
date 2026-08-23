#!/usr/bin/env python3
"""Merge upload state snapshots without dropping concurrent updates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DESCRIPTION_FIELDS = {"description_updated_at", "description_text_hash", "description_source"}


def upload_key(upload: dict) -> tuple[str, int] | tuple[int, int, int]:
    if upload.get("series_id") is not None and upload.get("season") is not None and upload.get("episode") is not None:
        return (int(upload["series_id"]), int(upload["season"]), int(upload["episode"]))
    return ("episode_id", int(upload["episode_id"]))


def failure_key(failure: dict) -> tuple[int, int, str]:
    return (
        int(failure.get("episode_id") or 0),
        int(failure.get("source_id") or 0),
        str(failure.get("reason") or ""),
    )


def merge_upload(current: dict, incoming: dict) -> dict:
    merged = {**current, **incoming}
    for field in DESCRIPTION_FIELDS:
        current_value = current.get(field)
        incoming_value = incoming.get(field)
        if current_value and not incoming_value:
            merged[field] = current_value
    if current.get("description_updated_at") and not incoming.get("description_updated_at"):
        if current.get("description_source"):
            merged["description_source"] = current["description_source"]
    return merged


def account_matches(upload: dict, upload_account: str | None) -> bool:
    if not upload_account:
        return True
    return upload.get("upload_account") == upload_account


def merge_state(current: dict, incoming: dict, *, upload_account: str | None = None) -> dict:
    merged = {"schema_version": current.get("schema_version") or incoming.get("schema_version") or 1}

    uploads_by_key: dict[tuple[str, int] | tuple[int, int, int], dict] = {}
    order: list[tuple[str, int] | tuple[int, int, int]] = []
    for source in (current.get("uploads") or []):
        if not account_matches(source, upload_account):
            continue
        key = upload_key(source)
        if key not in uploads_by_key:
            order.append(key)
        uploads_by_key[key] = source
    for source in (incoming.get("uploads") or []):
        if not account_matches(source, upload_account):
            continue
        key = upload_key(source)
        if key not in uploads_by_key:
            order.append(key)
            uploads_by_key[key] = source
        else:
            uploads_by_key[key] = merge_upload(uploads_by_key[key], source)
    merged["uploads"] = [uploads_by_key[key] for key in order]

    failures_by_key: dict[tuple[int, int, str], dict] = {}
    failure_order: list[tuple[int, int, str]] = []
    for source in (current.get("failed_attempts") or []):
        key = failure_key(source)
        if key not in failures_by_key:
            failure_order.append(key)
        failures_by_key[key] = source
    for source in (incoming.get("failed_attempts") or []):
        key = failure_key(source)
        if key not in failures_by_key:
            failure_order.append(key)
            failures_by_key[key] = source
        elif (source.get("failed_at") or "") > (failures_by_key.get(key, {}).get("failed_at") or ""):
            failures_by_key[key] = source
    merged["failed_attempts"] = [failures_by_key[key] for key in failure_order]

    last_updated = max(current.get("last_updated") or "", incoming.get("last_updated") or "")
    if last_updated:
        merged["last_updated"] = last_updated
    return merged


def load_state(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {"schema_version": 1, "uploads": [], "failed_attempts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--incoming", required=True, type=Path)
    parser.add_argument("--upload-account", choices=("primary", "serialy"))
    args = parser.parse_args()

    save_state(
        args.target,
        merge_state(load_state(args.target), load_state(args.incoming), upload_account=args.upload_account),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
