#!/usr/bin/env python3
"""Compact prepared description JSONL so it stays below GitHub blob limits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_MAX_BYTES = 95_000_000


def row_key(row: dict) -> tuple[str, int] | None:
    kind = row.get("kind")
    if kind == "episode" and row.get("episode_id") is not None:
        return ("episode", int(row["episode_id"]))
    if kind == "series" and row.get("series_id") is not None:
        return ("series", int(row["series_id"]))
    return None


def encoded_size(row: dict) -> int:
    return len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1


def load_rows(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def compact_rows(rows: list[dict], *, keep_errors: int, max_bytes: int) -> list[dict]:
    latest_ok_by_entity: dict[tuple[str, int], tuple[int, dict]] = {}
    recent_errors: list[tuple[int, dict]] = []

    for index, row in enumerate(rows):
        if row.get("status") == "ok":
            key = row_key(row)
            if key is not None:
                latest_ok_by_entity[key] = (index, row)
        elif keep_errors > 0:
            recent_errors.append((index, row))

    kept = list(latest_ok_by_entity.values())
    kept.extend(recent_errors[-keep_errors:])
    kept.sort(key=lambda item: item[0])

    out: list[dict] = []
    total = 0
    for _, row in kept:
        size = encoded_size(row)
        if total + size > max_bytes:
            if row.get("status") != "ok":
                continue
            # If unique successful descriptions ever exceed the budget, keep
            # newest rows instead of creating an unpushable blob.
            while out and total + size > max_bytes:
                removed = out.pop(0)
                total -= encoded_size(removed)
        if total + size <= max_bytes:
            out.append(row)
            total += size
    return out


def save_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="plans/descriptions.jsonl", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--keep-errors", type=int, default=200)
    args = parser.parse_args()

    before_size = args.path.stat().st_size if args.path.exists() else 0
    rows = load_rows(args.path)
    compacted = compact_rows(rows, keep_errors=max(0, args.keep_errors), max_bytes=args.max_bytes)
    save_rows(args.path, compacted)
    after_size = args.path.stat().st_size if args.path.exists() else 0
    print(
        "compacted descriptions "
        f"rows={len(rows)}->{len(compacted)} "
        f"bytes={before_size}->{after_size} "
        f"limit={args.max_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
