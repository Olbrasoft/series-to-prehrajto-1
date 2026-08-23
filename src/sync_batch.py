#!/usr/bin/env python3
"""Upload a batch of series episodes to the serialy.prehrajto account."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download import MAX_FILE_SIZE, DownloadError, download_to, head_size  # noqa: E402
from description_quality import is_valid_generated_description  # noqa: E402
from language_checks import has_probable_czech, whisper_language  # noqa: E402
from pick_next_episode import BACKLOG, NUM_SHARDS, SHARD_ID, STATE, load_backlog, load_state, pick_next  # noqa: E402
from prehrajto_upload import login, upload_video  # noqa: E402
from resolve_stream import ResolveError, pick_best, resolve as resolve_stream  # noqa: E402
from upload_state_merge import merge_state  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "state" / (f"sync-shard-{SHARD_ID}.log" if NUM_SHARDS > 1 else "sync.log")
TMP_DIR = Path("/tmp")
DESCRIPTIONS = REPO_ROOT / "plans" / "descriptions.jsonl"
PREPARED_SOURCES = REPO_ROOT / "plans" / "prepared-episodes.jsonl"
UPLOAD_ACCOUNT = os.environ.get("PREHRAJTO_ACCOUNT", "primary")
MIN_UPLOAD_FILE_SIZE = 300 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("SYNC_DOWNLOAD_TIMEOUT_SECONDS", "360"))
DOWNLOAD_MIN_SPEED_BYTES = int(os.environ.get("SYNC_DOWNLOAD_MIN_SPEED_BYTES", "250000"))
DOWNLOAD_SPEED_TIME_SECONDS = int(os.environ.get("SYNC_DOWNLOAD_SPEED_TIME_SECONDS", "90"))


def log(message: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_state(state: dict) -> None:
    state["last_updated"] = now_iso()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def push_state(reason: str) -> None:
    if not os.environ.get("CI"):
        return
    try:
        state_rel = str(STATE.relative_to(REPO_ROOT))
        state_snapshot = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() and STATE.stat().st_size > 0 else {}
        tag = f"{UPLOAD_ACCOUNT} shard {SHARD_ID}/{NUM_SHARDS}" if NUM_SHARDS > 1 else f"{UPLOAD_ACCOUNT} sync"
        for _ in range(5):
            subprocess.run(["git", "fetch", "origin", "main"], check=False)
            subprocess.run(["git", "reset", "--hard", "origin/main"], check=False)
            STATE.parent.mkdir(parents=True, exist_ok=True)
            current_state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() and STATE.stat().st_size > 0 else {}
            merged_state = merge_state(current_state, state_snapshot, upload_account=UPLOAD_ACCOUNT)
            STATE.write_text(json.dumps(merged_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", state_rel], check=True)
            if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", f"chore({tag}): {reason}"], check=True)
            if subprocess.run(["git", "push", "origin", "HEAD:main"], check=False).returncode == 0:
                return
            time.sleep(2)
        log("push_state failed after retries; continuing")
    except Exception as exc:
        log(f"push_state non-fatal: {type(exc).__name__}: {exc}")


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:"<>|*?]', "_", name)[:180]


def load_description_plans(path: Path = DESCRIPTIONS) -> dict[str, dict[int, dict]]:
    plans: dict[str, dict[int, dict]] = {"series": {}, "episode": {}}
    if not path.exists():
        return plans
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok":
                continue
            if not is_valid_generated_description(row.get("generated_description") or ""):
                continue
            kind = row.get("kind")
            if kind == "series":
                plans["series"][int(row["series_id"])] = row
            elif kind == "episode":
                plans["episode"][int(row["episode_id"])] = row
    return plans


def prepared_description(episode: dict, plans: dict[str, dict[int, dict]]) -> str | None:
    upload_job_description = ((episode.get("upload_manifest") or {}).get("upload_job") or {}).get("description")
    if upload_job_description:
        return upload_job_description
    manifest_description = ((episode.get("upload_manifest") or {}).get("description") or {}).get("text")
    if manifest_description:
        return manifest_description
    ep = plans["episode"].get(int(episode["episode_id"]))
    if ep and ep.get("generated_description"):
        return ep["generated_description"]
    series = plans["series"].get(int(episode["series_id"]))
    if series and series.get("generated_description"):
        return series["generated_description"]
    return None


def load_source_plans(path: Path = PREPARED_SOURCES) -> dict[int, dict]:
    plans: dict[int, dict] = {}
    if not path.exists():
        return plans
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            plans[int(row["episode_id"])] = row
    return plans


def manifest_selected_source(episode: dict) -> dict | None:
    source_plan = ((episode.get("upload_manifest") or {}).get("source_plan") or {})
    selected_id = source_plan.get("selected_source_id")
    if selected_id is None:
        return None
    return {
        "source_id": int(selected_id),
        "verdict": source_plan.get("verdict"),
        "detected_by": source_plan.get("detected_by"),
    }


def apply_source_plan(episode: dict, source_plans: dict[int, dict], *, require_source_plan: bool) -> dict | None:
    selected = manifest_selected_source(episode)
    plan = source_plans.get(int(episode["episode_id"]))
    if not plan:
        if require_source_plan and not selected:
            log(f"episode episode_id={episode['episode_id']} SKIP missing prepared source plan")
            return None
        if not selected:
            return episode
    elif not selected:
        selected = plan.get("selected_source")

    if require_source_plan and plan and not ((episode.get("upload_manifest") or {}).get("source_plan")) and not plan.get("upload_ready"):
        verdict = selected.get("verdict") if selected else "none"
        log(f"episode episode_id={episode['episode_id']} SKIP source plan not upload-ready verdict={verdict}")
        return None
    if not selected:
        return None if require_source_plan else episode
    selected_id = int(selected["source_id"])
    candidates = episode.get("candidates") or []
    ordered = [c for c in candidates if int(c["source_id"]) == selected_id]
    ordered.extend(c for c in candidates if int(c["source_id"]) != selected_id)
    if require_source_plan and not ordered:
        log(f"episode episode_id={episode['episode_id']} SKIP selected source_id={selected_id} not in upload backlog")
        return None
    planned = {**episode, "candidates": ordered or candidates}
    planned["source_plan_verdict"] = selected.get("verdict")
    planned["source_plan_detected_by"] = selected.get("detected_by")
    return planned


def record_failure(
    state: dict,
    episode: dict,
    candidate: dict,
    reason: str,
    *,
    permanent: bool,
    timing: dict | None = None,
    push_reason: str | None = None,
) -> None:
    entry = {
        "episode_id": episode["episode_id"],
        "series_id": episode["series_id"],
        "display_name": episode["display_name"],
        "source_id": candidate["source_id"],
        "external_id": candidate.get("external_id"),
        "url": candidate.get("url"),
        "reason": reason,
        "permanent": permanent,
        "failed_at": now_iso(),
    }
    if timing:
        entry["timing"] = timing
    state.setdefault("failed_attempts", []).append(entry)
    save_state(state)
    if push_reason:
        push_state(push_reason)


def try_candidate(episode: dict, candidate: dict, session, state: dict, *, allow_subtitles: bool, description: str) -> bool:
    ok, reason = has_probable_czech(candidate, allow_subtitles=allow_subtitles)
    if not ok:
        log(f"  skip source_id={candidate['source_id']} language={reason}")
        record_failure(state, episode, candidate, f"language_rejected: {reason}", permanent=True)
        return False

    log(f"  candidate source_id={candidate['source_id']} lang={candidate.get('lang_class')} url={candidate['url']}")
    t = time.monotonic()
    try:
        resolved = resolve_stream(candidate["url"])
        best = pick_best(resolved.videos, prefer=(1080, 720))
    except ResolveError as exc:
        log(f"  resolve FAILED: {exc} permanent={exc.permanent}")
        record_failure(state, episode, candidate, f"resolve_failed: {exc}", permanent=exc.permanent)
        return False
    resolve_sec = round(time.monotonic() - t, 1)
    log(f"  resolved in {resolve_sec}s -> {best.label}")

    expected = head_size(best.url)
    if expected is not None and expected > MAX_FILE_SIZE:
        log(f"  oversize: {expected} B > {MAX_FILE_SIZE} B")
        record_failure(state, episode, candidate, f"oversize: {expected} B", permanent=True)
        return False
    if expected is not None and expected < MIN_UPLOAD_FILE_SIZE:
        log(f"  undersize head: {expected} B < {MIN_UPLOAD_FILE_SIZE} B")
        record_failure(state, episode, candidate, f"undersize: {expected} B (head)", permanent=True)
        return False

    tmp_path = TMP_DIR / f"{safe_filename(episode['display_name'])}.mp4"
    t = time.monotonic()
    try:
        size = download_to(
            best.url,
            tmp_path,
            timeout_sec=DOWNLOAD_TIMEOUT_SECONDS,
            min_speed_bytes=DOWNLOAD_MIN_SPEED_BYTES,
            speed_time_sec=DOWNLOAD_SPEED_TIME_SECONDS,
        )
    except DownloadError as exc:
        dl_sec = round(time.monotonic() - t, 1)
        log(f"  download FAILED after {dl_sec}s: {exc}")
        record_failure(
            state,
            episode,
            candidate,
            f"download_failed: {exc}",
            permanent=False,
            timing={"resolve_sec": resolve_sec, "download_sec": dl_sec},
            push_reason=f"temporarily skip {episode['display_name']}",
        )
        tmp_path.unlink(missing_ok=True)
        return False
    dl_sec = round(time.monotonic() - t, 1)
    log(f"  downloaded {size / 1_000_000:,.1f} MB in {dl_sec}s")
    if size < MIN_UPLOAD_FILE_SIZE:
        log(
            f"  undersize: {size} B < {MIN_UPLOAD_FILE_SIZE} B; "
            "skipping"
        )
        record_failure(
            state,
            episode,
            candidate,
            f"undersize: {size} B",
            permanent=True,
            timing={"resolve_sec": resolve_sec, "download_sec": dl_sec},
        )
        tmp_path.unlink(missing_ok=True)
        return False

    lang, prob, whisper_status = whisper_language(tmp_path)
    if lang and lang not in {"cs", "cz", "sk"}:
        log(f"  whisper rejected language={lang} prob={prob}")
        record_failure(state, episode, candidate, f"whisper_rejected: {lang} {prob}", permanent=True, timing={"resolve_sec": resolve_sec, "download_sec": dl_sec})
        tmp_path.unlink(missing_ok=True)
        return False
    if whisper_status != "disabled":
        log(f"  whisper status={whisper_status} language={lang} prob={prob}")

    t = time.monotonic()
    try:
        video_id = upload_video(session, tmp_path, display_name=episode["display_name"], description=description)
    except Exception as exc:
        up_sec = round(time.monotonic() - t, 1)
        log(f"  upload FAILED after {up_sec}s: {exc}")
        record_failure(state, episode, candidate, f"upload_failed: {type(exc).__name__}: {exc}", permanent=False, timing={"resolve_sec": resolve_sec, "download_sec": dl_sec, "upload_sec": up_sec})
        return False
    finally:
        tmp_path.unlink(missing_ok=True)
    up_sec = round(time.monotonic() - t, 1)
    log(f"  upload done video_id={video_id} in {up_sec}s")

    state.setdefault("uploads", []).append(
        {
            "episode_id": episode["episode_id"],
            "series_id": episode["series_id"],
            "series_title": episode["series_title"],
            "season": episode["season"],
            "episode": episode["episode"],
            "episode_code": episode.get("episode_code") or f"S{int(episode.get('season', 0)):02d}E{int(episode.get('episode', 0)):02d}",
            "display_name": episode["display_name"],
            "upload_account": UPLOAD_ACCOUNT,
            "source_id": candidate["source_id"],
            "external_id": candidate.get("external_id"),
            "source_url": candidate.get("url"),
            "source_lang_class": candidate.get("lang_class"),
            "source_resolution": best.label,
            "description_source": episode.get("description_source"),
            "prehrajto_video_id": video_id,
            "size_bytes": size,
            "uploaded_at": now_iso(),
            "timing": {"resolve_sec": resolve_sec, "download_sec": dl_sec, "upload_sec": up_sec},
        }
    )
    save_state(state)
    push_state(f"+{episode['display_name']}")
    return True


def process_episode(episode: dict, session, state: dict, *, allow_subtitles: bool, description_plans: dict[str, dict[int, dict]], require_description: bool) -> bool:
    upload_job = (episode.get("upload_manifest") or {}).get("upload_job") or {}
    if upload_job.get("display_name"):
        episode["display_name"] = upload_job["display_name"]
    log(f"episode episode_id={episode['episode_id']} name={episode['display_name']!r} candidates={len(episode['candidates'])}")
    description = prepared_description(episode, description_plans)
    if description:
        episode["description_source"] = "gemma_episode_or_series"
    elif require_description:
        log(f"episode episode_id={episode['episode_id']} SKIP missing prepared Gemma description")
        return False
    else:
        description = episode.get("description") or ""
        episode["description_source"] = "backlog_fallback"
    candidates = episode.get("candidates") or []
    if not candidates:
        log(f"episode episode_id={episode['episode_id']} SKIP no candidates")
        return False
    candidate = candidates[0]
    if try_candidate(episode, candidate, session, state, allow_subtitles=allow_subtitles, description=description):
        return True
    log(f"episode episode_id={episode['episode_id']} exhausted (single candidate)")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument(
        "--max-episode-attempts",
        type=int,
        default=None,
        help="Maximum episodes to inspect while trying to reach --count successful uploads.",
    )
    ap.add_argument("--allow-subtitles", action="store_true")
    ap.add_argument("--require-description", action="store_true", default=os.environ.get("REQUIRE_PREPARED_DESCRIPTIONS") == "1")
    ap.add_argument("--require-source-plan", action="store_true", default=os.environ.get("REQUIRE_PREPARED_SOURCES") == "1")
    args = ap.parse_args()
    max_episode_attempts = args.max_episode_attempts or max(args.count, args.count * 6)

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        log("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required")
        return 2
    if not BACKLOG.exists():
        log(f"ERROR: backlog missing: {BACKLOG}")
        return 2

    rows = load_backlog()
    state = load_state()
    description_plans = load_description_plans()
    source_plans = load_source_plans()
    log(
        f"batch-start account={UPLOAD_ACCOUNT} count={args.count} max_episode_attempts={max_episode_attempts} "
        f"backlog={len(rows)} uploads={len(state.get('uploads', []))} failed={len(state.get('failed_attempts', []))}"
    )
    log(f"description-plans series={len(description_plans['series'])} episodes={len(description_plans['episode'])} require={args.require_description}")
    log(f"source-plans episodes={len(source_plans)} require={args.require_source_plan}")
    log("login")
    session = login(email, password)
    log("login done")

    ok = bad = 0
    attempted: set[int] = set()
    for index in range(max_episode_attempts):
        if ok >= args.count:
            break
        log(f"iteration {index + 1}/{max_episode_attempts} ok={ok}/{args.count}")
        episode = pick_next(state, rows, attempted)
        if not episode:
            log("backlog exhausted")
            break
        attempted.add(int(episode["episode_id"]))
        episode = apply_source_plan(episode, source_plans, require_source_plan=args.require_source_plan)
        if episode is None:
            bad += 1
            continue
        if process_episode(episode, session, state, allow_subtitles=args.allow_subtitles, description_plans=description_plans, require_description=args.require_description):
            ok += 1
        else:
            bad += 1
    log(f"batch-end ok={ok} failed={bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
