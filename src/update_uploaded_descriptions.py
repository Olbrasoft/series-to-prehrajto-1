#!/usr/bin/env python3
"""Apply prepared Gemma descriptions to already uploaded Přehraj.to videos."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prehrajto_upload import login  # noqa: E402
from sync_batch import load_description_plans, prepared_description  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state" / "uploaded.json"
LOG = REPO / "state" / "description-updates.log"
EDIT_URL = "https://prehraj.to/profil/nahrana-videa"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def state_paths(pattern: str) -> list[Path]:
    paths = sorted(REPO.glob(pattern))
    return [path for path in paths if path.is_file() and path.stat().st_size > 0]


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def change_description(session: requests.Session, video_id: int, name: str, description: str) -> tuple[bool, str]:
    params = {
        "uploadedVideoListing-videoId": str(video_id),
        "do": "uploadedVideoListing-changeVideoNameAndVideoDescription",
        "uploadedVideoListing-name": name,
        "uploadedVideoListing-desc": description,
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
        "Referer": "https://prehraj.to/profil/nahrana-videa",
    }
    resp = session.get(EDIT_URL, params=params, headers=headers, timeout=30, allow_redirects=False)
    return resp.status_code == 200, f"http={resp.status_code} len={len(resp.text)}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--throttle", type=float, default=0.5)
    ap.add_argument(
        "--state-glob",
        default="state/uploaded*.json",
        help="Repository-relative glob for upload state files to update.",
    )
    args = ap.parse_args()

    plans = load_description_plans()
    tasks = []
    states: dict[Path, dict] = {}
    paths = state_paths(args.state_glob)
    for path in paths:
        state = load_state(path)
        states[path] = state
        for upload in state.get("uploads", []):
            description = prepared_description(upload, plans)
            if not description:
                continue
            desc_hash = text_hash(description)
            if upload.get("description_updated_at") and upload.get("description_text_hash") == desc_hash:
                continue
            tasks.append((path, upload, description, desc_hash))
    if args.limit:
        tasks = tasks[: args.limit]

    log(f"start state_files={len(paths)} tasks={len(tasks)} dry_run={args.dry_run}")
    if args.dry_run:
        for path, upload, description, _desc_hash in tasks[:5]:
            log(f"DRY state={path.name} video_id={upload['prehrajto_video_id']} name={upload['display_name']} desc_len={len(description)}")
        return 0
    if not tasks:
        return 0

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        print("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required", file=sys.stderr)
        return 2

    session = login(email, password)
    ok = fail = 0
    for index, (path, upload, description, desc_hash) in enumerate(tasks, 1):
        success, info = change_description(session, int(upload["prehrajto_video_id"]), upload["display_name"], description)
        if success:
            ok += 1
            upload["description_updated_at"] = now_iso()
            upload["description_source"] = "gemma_prepared"
            upload["description_text_hash"] = desc_hash
            log(f"OK {index}/{len(tasks)} state={path.name} video_id={upload['prehrajto_video_id']} {info}")
            save_state(path, states[path])
        else:
            fail += 1
            log(f"FAIL {index}/{len(tasks)} state={path.name} video_id={upload['prehrajto_video_id']} {info}")
        if args.throttle and index < len(tasks):
            time.sleep(args.throttle)
    log(f"done ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
