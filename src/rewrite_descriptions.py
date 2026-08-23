#!/usr/bin/env python3
"""Rewrite episode descriptions with Gemini/Gemma-compatible API keys."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemma-4-31b-it")
DEFAULT_THINKING_BUDGET = os.environ.get("GEMINI_THINKING_BUDGET", "0")


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    opener = gzip.open if path.suffix == ".gz" else open
    path.parent.mkdir(parents=True, exist_ok=True)
    with opener(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def api_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    keys = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
    if not keys:
        raise RuntimeError("GEMINI_API_KEY or GEMINI_API_KEYS is required")
    return keys


def rewrite_one(row: dict, key: str, model: str) -> str:
    source = row.get("source_description") or row.get("description") or ""
    if not source.strip():
        return ""
    title = row.get("display_name") or row.get("series_title") or "Epizoda"
    prompt = (
        "Přepiš následující český popis seriálové epizody tak, aby nebyl stejný "
        "jako zdroj, ale zachoval fakta. Piš česky, stručně, bez spoilerů, 2 až 4 věty. "
        f"Název: {title}\nZdrojový popis:\n{source}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    generation_config: dict = {}
    if DEFAULT_THINKING_BUDGET.strip() and not model.startswith("gemma-"):
        generation_config["thinkingConfig"] = {"thinkingBudget": int(DEFAULT_THINKING_BUDGET)}
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation_config}
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="input", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--out", default="backlog/series-episodes.described.jsonl.gz")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    rows = load_jsonl(Path(args.input))
    if args.limit:
        rows = rows[: args.limit]
    keys = api_keys()

    def task(index_row: tuple[int, dict]) -> tuple[int, str]:
        index, row = index_row
        return index, rewrite_one(row, keys[index % len(keys)], args.model)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [ex.submit(task, item) for item in enumerate(rows)]
        for fut in as_completed(futures):
            index, text = fut.result()
            if text:
                rows[index]["description"] = text
                rows[index]["description_rewritten"] = True
            print(f"rewritten {index + 1}/{len(rows)}", file=sys.stderr)

    write_jsonl(Path(args.out), rows)
    print(f"Wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
