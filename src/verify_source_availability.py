#!/usr/bin/env python3
"""Verify prepared Přehraj.to episode sources resolve and look uploadable.

This script is intentionally small: it reads URLs from an upload manifest (or
plain URL arguments), resolves each Přehraj.to detail page the same way the
uploader does, picks the best stream, and checks that the stream advertises a
large enough file through HEAD.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download import head_size  # noqa: E402
from resolve_stream import ResolveError, pick_best, resolve  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIN_BYTES = 300 * 1024 * 1024
DEFAULT_MIN_RESOLUTION = 1080


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def manifest_url(row: dict[str, Any]) -> str | None:
    upload_job = (row.get("upload_manifest") or {}).get("upload_job") or {}
    if upload_job.get("source_url"):
        return str(upload_job["source_url"])
    source_plan = (row.get("upload_manifest") or {}).get("source_plan") or {}
    if source_plan.get("source_url"):
        return str(source_plan["source_url"])
    candidates = row.get("candidates") or []
    if candidates and candidates[0].get("url"):
        return str(candidates[0]["url"])
    return None


def manifest_items(path: Path, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in load_jsonl(path):
        url = manifest_url(row)
        if not url:
            continue
        items.append(
            {
                "episode_id": row.get("episode_id"),
                "series_title": row.get("series_title"),
                "season": row.get("season"),
                "episode": row.get("episode"),
                "display_name": ((row.get("upload_manifest") or {}).get("upload_job") or {}).get("display_name")
                or row.get("display_name"),
                "source_url": url,
            }
        )
        if limit and len(items) >= limit:
            break
    return items


def merge_existing_results(path: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(path):
        url = row.get("source_url")
        if url:
            merged[str(url)] = row
    for row in results:
        url = row.get("source_url")
        if url:
            merged[str(url)] = row
    return list(merged.values())


def url_items(urls: list[str]) -> list[dict[str, Any]]:
    return [{"source_url": url} for url in urls]


def verify_item(item: dict[str, Any], *, min_bytes: int, min_resolution: int, timeout: int) -> dict[str, Any]:
    url = item["source_url"]
    result = {**item, "ok": False}
    try:
        resolved = resolve(url, timeout=timeout)
        best = pick_best(resolved.videos, prefer=(1080, 720))
        size = head_size(best.url, timeout_sec=timeout)
        result.update(
            {
                "resolved": True,
                "video_id": resolved.video_id,
                "resolved_name": resolved.name,
                "duration_sec": resolved.duration_sec,
                "fetch_via": resolved.fetch_via,
                "best_resolution": best.res,
                "best_label": best.label,
                "head_size_bytes": size,
                "variant_count": len(resolved.videos),
                "variants": [{"res": v.res, "label": v.label} for v in resolved.videos],
            }
        )
        if best.res < min_resolution:
            result["reason"] = f"resolution_below_{min_resolution}"
        elif size is None:
            result["reason"] = "missing_content_length"
        elif size < min_bytes:
            result["reason"] = f"file_below_{min_bytes}_bytes"
        else:
            result["ok"] = True
            result["reason"] = "ok"
    except ResolveError as exc:
        result.update({"resolved": False, "reason": "resolve_error", "error": str(exc), "permanent": exc.permanent})
    except Exception as exc:  # noqa: BLE001 - report operational diagnostics.
        result.update({"resolved": False, "reason": type(exc).__name__, "error": str(exc)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="*", help="Optional direct Přehraj.to source URLs.")
    parser.add_argument("--manifest", default="manifests/upload-ready.jsonl.gz")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--min-mb", type=int, default=300)
    parser.add_argument("--min-resolution", type=int, default=1080)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--out", default="reports/source-availability.jsonl")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    min_bytes = args.min_mb * 1024 * 1024
    items = url_items(args.urls) if args.urls else manifest_items(REPO_ROOT / args.manifest, args.limit)
    if not items:
        print("No source URLs to verify", file=sys.stderr)
        return 2

    results = [
        verify_item(item, min_bytes=min_bytes, min_resolution=args.min_resolution, timeout=args.timeout)
        for item in items
    ]
    out_path = REPO_ROOT / args.out
    results = merge_existing_results(out_path, results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in results:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok = sum(1 for row in results if row.get("ok"))
    print(f"verified_ok={ok} total={len(results)} out={args.out}")
    for row in results:
        size = row.get("head_size_bytes")
        size_mb = f"{size / 1024 / 1024:.1f} MiB" if isinstance(size, int) else "unknown"
        print(
            f"{'OK' if row.get('ok') else 'FAIL'} "
            f"{row.get('display_name') or row.get('source_url')} "
            f"res={row.get('best_resolution')} size={size_mb} reason={row.get('reason')}"
        )
    if args.fail_on_error and ok != len(results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
