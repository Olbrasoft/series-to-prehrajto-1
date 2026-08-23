#!/usr/bin/env python3
"""Attach Czech subtitles to already uploaded subtitle-only episodes.

Prehraj.to accepts SRT uploads, then serves processed subtitles as VTT tracks.
WEBVTT uploads can stay stuck in "Zpracovává se" forever, so every provider
VTT is converted to strict CRLF SRT with a unique short filename.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prehrajto_search import search_pages  # noqa: E402
from prehrajto_upload import ACCEPT_LANG, SEC_CH_UA, login  # noqa: E402
from resolve_stream import ResolveError, ResolvedUpload, resolve  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROFILE_URL = "https://prehraj.to/profil/nahrana-videa"
DETAIL_BASE = "https://prehraj.to"
SAFE_LANGS = {"cs", "cz", "cze", "ces", "česky", "cesky"}
VIDEO_MARKER_RE = re.compile(r'id="snippet-uploadedVideoListing-video-(\d+)"')
REMOVE_RE = re.compile(
    r"uploadedVideoListing-videoId=(\d+)[^\"']*?"
    r"uploadedVideoListing-subtitleId=(\d+)[^\"']*?"
    r"do=uploadedVideoListing-removeSubtitle"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {message}", flush=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_latest_status(path: Path) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in load_jsonl(path):
        try:
            latest[int(row["episode_id"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return latest


def row_pending(row: dict) -> bool:
    status = str(row.get("status") or "missing")
    subtitle_status = str(row.get("subtitle_status") or "")
    return status not in {"done", "completed", "subtitle_attached", "closed"} and subtitle_status != "done"


def episode_key(row: dict) -> tuple[int, int, int, int]:
    return (
        int(row.get("series_id") or 0),
        int(row.get("season") or 0),
        int(row.get("episode") or 0),
        int(row.get("episode_id") or 0),
    )


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def candidate_matches_series(row: dict, title: str) -> bool:
    series = normalize_title(str(row.get("series_title") or ""))
    candidate = normalize_title(title)
    if not series or not candidate:
        return False
    if candidate == series or candidate.startswith(series + " "):
        return True
    # Long localized aliases can prefix the canonical title, e.g.
    # "Anna zo Zeleného domu Z - Anne s E na konci". Keep short titles strict
    # so broad names such as "Bloodline" do not match "Tekken Bloodline".
    if len(series.split()) >= 4 and f" {series} " in f" {candidate} ":
        return True
    return False


def load_uploads(paths: list[Path]) -> dict[int, dict]:
    by_episode: dict[int, dict] = {}
    for path in paths:
        state = load_json(path)
        for upload in state.get("uploads", []):
            try:
                episode_id = int(upload["episode_id"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(upload.get("display_name") or "")
            if "Titulky" not in name:
                continue
            by_episode[episode_id] = upload
    return by_episode


def profile_page_url(page: int) -> str:
    if page <= 1:
        return PROFILE_URL
    return f"{PROFILE_URL}?uploadedVideoListing-visualPaginator-page={page}"


def extract_blocks(page_html: str, page: int) -> dict[int, dict]:
    markers = list(VIDEO_MARKER_RE.finditer(page_html))
    blocks: dict[int, dict] = {}
    for index, marker in enumerate(markers):
        video_id = int(marker.group(1))
        end = markers[index + 1].start() if index + 1 < len(markers) else len(page_html)
        block = page_html[marker.start() : end]
        title_match = re.search(
            rf'id="snippet-uploadedVideoListing-videoName-{video_id}">\s*(.*?)\s*</h3>',
            block,
            re.DOTALL,
        )
        title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip() if title_match else ""
        detail_match = re.search(r'<a[^>]+href="(?P<href>/[^"]+/[0-9a-f]{12,32})"[^>]*>\s*Detail souboru\s*</a>', block)
        count_match = re.search(rf'subtitlescount-{video_id}">\((\d+)\)', block)
        remove_ids = [int(sid) for vid, sid in REMOVE_RE.findall(block) if int(vid) == video_id]
        blocks[video_id] = {
            "page": page,
            "title": title,
            "processing": "Zpracovává se" in title,
            "detail_url": urljoin(DETAIL_BASE, html.unescape(detail_match.group("href"))) if detail_match else None,
            "subtitle_count": int(count_match.group(1)) if count_match else 0,
            "remove_subtitle_ids": remove_ids,
        }
    return blocks


def scan_profile(session: requests.Session, wanted_video_ids: set[int], max_pages: int) -> dict[int, dict]:
    found: dict[int, dict] = {}
    empty_pages = 0
    for page in range(1, max_pages + 1):
        resp = session.get(profile_page_url(page), headers={"Referer": PROFILE_URL}, timeout=30)
        resp.raise_for_status()
        blocks = extract_blocks(resp.text, page)
        if not blocks:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        for video_id, info in blocks.items():
            if video_id in wanted_video_ids:
                found[video_id] = info
        missing = wanted_video_ids - set(found)
        log(f"profile page={page} found={len(found)}/{len(wanted_video_ids)}")
        if not missing:
            break
    return found


def detect_subtitle_format(content: bytes) -> tuple[str, str]:
    head = content.lstrip(b"\xef\xbb\xbf").lstrip()
    if head[:6] == b"WEBVTT":
        return ".vtt", "text/vtt"
    if head[:11].lower().startswith(b"[script info"):
        return ".ass", "text/x-ass"
    return ".srt", "application/x-subrip"


def vtt_to_srt(vtt_bytes: bytes) -> bytes:
    text = vtt_bytes.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    lines = text.split("\n")
    if lines and lines[0].startswith("WEBVTT"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    body = "\n".join(lines)
    body = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", body)
    timestamp_re = re.compile(r"^(\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}).*$")
    out: list[str] = []
    number = 1
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or " --> " not in block:
            continue
        lines = block.split("\n")
        if lines and " --> " not in lines[0]:
            lines = lines[1:]
        if not lines:
            continue
        match = timestamp_re.match(lines[0])
        if match:
            lines[0] = match.group(1)
        out.append(f"{number}\r\n" + "\r\n".join(lines))
        number += 1
    return ("\r\n\r\n".join(out) + "\r\n").encode("utf-8")


def pick_czech_track(resolved: ResolvedUpload) -> str | None:
    for track in resolved.tracks:
        if (track.lang or "").strip().lower() in SAFE_LANGS:
            return track.url
    return resolved.tracks[0].url if resolved.tracks else None


def source_with_subtitles(row: dict) -> tuple[str | None, str | None]:
    source_url = row.get("source_url")
    if source_url:
        try:
            resolved = resolve(str(source_url), max_retries=1)
            if resolved.name and not candidate_matches_series(row, resolved.name):
                log(
                    f"skip source title mismatch episode_id={row.get('episode_id')} "
                    f"series={row.get('series_title')!r} title={resolved.name!r}"
                )
                return None, None
            track = pick_czech_track(resolved)
            if track:
                return str(source_url), track
        except Exception as exc:
            log(f"source resolve failed episode_id={row.get('episode_id')} {exc}")
    return None, None


def query_variants(row: dict) -> list[str]:
    series = str(row.get("series_title") or "").strip()
    season = int(row.get("season") or 0)
    episode = int(row.get("episode") or 0)
    code = str(row.get("episode_code") or f"S{season:02d}E{episode:02d}")
    variants = [
        f"{series} {code}",
        f"{series} {season}x{episode}",
        f"{series} {season:02d}x{episode:02d}",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for value in variants:
        value = " ".join(value.split())
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def target_query_variants(upload: dict) -> list[str]:
    display_name = str(upload.get("display_name") or "").strip()
    simplified = re.sub(r"\s+-\s+", " ", display_name)
    simplified = re.sub(r"\bCZ\s+Titulky\b", "", simplified, flags=re.IGNORECASE).strip()
    asciiish = re.sub(r"[^\w\s]+", " ", display_name, flags=re.UNICODE)
    variants = [display_name, simplified, asciiish]
    seen: set[str] = set()
    out: list[str] = []
    for value in variants:
        value = " ".join(value.split())
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def find_uploaded_detail(upload: dict, *, min_interval: float) -> dict | None:
    try:
        target_video_id = int(upload["prehrajto_video_id"])
    except (KeyError, TypeError, ValueError):
        return None
    for query in target_query_variants(upload):
        log(f"search target video_id={target_video_id} query={query!r}")
        try:
            pages = search_pages(query, max_pages=2, min_interval=min_interval, should_fetch_next=lambda results: True)
        except Exception as exc:
            log(f"target search failed video_id={target_video_id} query={query!r} {exc}")
            continue
        for item in [item for page in pages for item in page]:
            try:
                resolved = resolve(item.url, max_retries=1)
            except Exception as exc:
                log(f"target resolve failed video_id={target_video_id} candidate={item.url} {exc}")
                continue
            if resolved.video_id != target_video_id:
                continue
            return {
                "page": 1,
                "title": resolved.name or item.title,
                "processing": False,
                "detail_url": item.url,
                "subtitle_count": len(resolved.tracks),
                "remove_subtitle_ids": [],
                "resolved": resolved,
            }
    return None


def find_alternate_track(row: dict, target_duration: int | None, *, min_interval: float) -> tuple[str | None, str | None]:
    for query in query_variants(row):
        log(f"search subtitles episode_id={row.get('episode_id')} query={query!r}")
        try:
            pages = search_pages(query, max_pages=2, min_interval=min_interval, should_fetch_next=lambda results: True)
        except Exception as exc:
            log(f"search failed episode_id={row.get('episode_id')} query={query!r} {exc}")
            continue
        candidates = [item for page in pages for item in page]
        scored: list[tuple[int, str, str]] = []
        for item in candidates:
            if not candidate_matches_series(row, item.title):
                log(
                    f"skip title mismatch episode_id={row.get('episode_id')} "
                    f"series={row.get('series_title')!r} title={item.title!r}"
                )
                continue
            try:
                resolved = resolve(item.url, max_retries=1)
            except Exception:
                continue
            track = pick_czech_track(resolved)
            if not track:
                continue
            duration = resolved.duration_sec or item.duration_sec
            delta = abs(duration - target_duration) if duration and target_duration else 99999
            if delta > 20:
                continue
            scored.append((delta, item.url, track))
        if scored:
            scored.sort(key=lambda item: item[0])
            return scored[0][1], scored[0][2]
    return None, None


def fetch_subtitle(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    return resp.content


def upload_subtitle(session: requests.Session, video_id: int, page: int, content: bytes, suffix: str) -> requests.Response:
    filename = f"cs-{suffix}.srt"
    return session.post(
        f"{PROFILE_URL}?uploadedVideoListing-visualPaginator-page={page}&do=uploadedVideoListing-uploadSubtitles",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": ACCEPT_LANG,
            "Origin": "https://prehraj.to",
            "Referer": profile_page_url(page),
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        },
        files={"files[]": (filename, content, "application/x-subrip")},
        data={"video": str(video_id)},
        timeout=60,
        allow_redirects=False,
    )


def remove_subtitle(session: requests.Session, video_id: int, subtitle_id: int, page: int) -> requests.Response:
    return session.get(
        f"{PROFILE_URL}?uploadedVideoListing-videoId={video_id}"
        f"&uploadedVideoListing-subtitleId={subtitle_id}"
        f"&uploadedVideoListing-visualPaginator-page={page}"
        "&do=uploadedVideoListing-removeSubtitle",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": profile_page_url(page),
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
        allow_redirects=False,
    )


def verify_tracks(detail_url: str, timeout_sec: int) -> bool:
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            resolved = resolve(detail_url, max_retries=1)
            if resolved.tracks:
                return True
        except ResolveError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)


def status_row(row: dict, upload: dict, status: str, **extra) -> dict:
    out = {
        "checked_at": now_iso(),
        "status": status,
        "episode_id": row.get("episode_id"),
        "series_title": row.get("series_title"),
        "season": row.get("season"),
        "episode": row.get("episode"),
        "episode_code": row.get("episode_code"),
        "display_name": upload.get("display_name"),
        "prehrajto_video_id": upload.get("prehrajto_video_id"),
    }
    out.update(extra)
    return out


def build_tasks(args: argparse.Namespace, session: requests.Session | None) -> list[tuple[dict, dict, dict]]:
    followups = [row for row in load_jsonl(args.followup_file) if row_pending(row)]
    uploads = load_uploads(args.state_file)
    latest_status = load_latest_status(args.report_file)
    terminal_statuses = {
        "already_has_tracks",
        "source_track_not_found",
        "target_detail_missing",
        "target_not_found",
        "target_processing",
        "target_unresolved",
        "uploaded",
        "unsupported_subtitle_format",
    }
    matched: list[tuple[dict, dict]] = []
    for row in followups:
        if args.episode_id and int(row.get("episode_id") or 0) not in args.episode_id:
            continue
        previous = latest_status.get(int(row.get("episode_id") or 0))
        if previous and previous.get("status") in terminal_statuses and not args.retry_reported:
            continue
        upload = uploads.get(int(row.get("episode_id") or 0))
        if upload:
            matched.append((row, upload))
    tasks: list[tuple[dict, dict, dict]] = []
    for inspected, (row, upload) in enumerate(matched, 1):
        if args.max_rows and inspected > args.max_rows:
            log(f"stop max_rows={args.max_rows} inspected={inspected - 1}")
            break
        video_id = int(upload["prehrajto_video_id"])
        if args.lookup == "profile":
            if session is None:
                raise RuntimeError("profile lookup requires a logged-in session")
            profile = scan_profile(session, {video_id}, args.max_profile_pages)
            info = profile.get(video_id)
        else:
            info = find_uploaded_detail(upload, min_interval=args.search_min_interval)
        if not info:
            append_jsonl(args.report_file, status_row(row, upload, "target_not_found"))
            log(f"skip target not found episode_id={row.get('episode_id')} video_id={video_id}")
            continue
        if info["processing"]:
            append_jsonl(args.report_file, status_row(row, upload, "target_processing", detail_url=info.get("detail_url")))
            log(f"skip processing video_id={video_id} name={upload.get('display_name')!r}")
            continue
        detail_url = info.get("detail_url")
        if not detail_url:
            append_jsonl(args.report_file, status_row(row, upload, "target_detail_missing"))
            continue
        try:
            current = info.get("resolved") or resolve(detail_url, max_retries=1)
        except Exception as exc:
            append_jsonl(args.report_file, status_row(row, upload, "target_unresolved", detail_url=detail_url, reason=str(exc)))
            log(f"skip unresolved target video_id={video_id} {exc}")
            continue
        if current.tracks:
            append_jsonl(
                args.report_file,
                status_row(row, upload, "already_has_tracks", detail_url=detail_url, track_count=len(current.tracks)),
            )
            log(f"skip already has tracks video_id={video_id} tracks={len(current.tracks)}")
            continue
        tasks.append((row, upload, info))
        if args.limit and len(tasks) >= args.limit:
            break
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--max-profile-pages", type=int, default=40)
    ap.add_argument("--verify-timeout", type=int, default=70)
    ap.add_argument("--search-min-interval", type=float, default=10.0)
    ap.add_argument("--lookup", choices=["public", "profile"], default="public")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--retry-reported", action="store_true")
    ap.add_argument("--episode-id", type=int, action="append", default=[])
    ap.add_argument("--followup-file", type=Path, default=REPO / "plans/subtitle-followup-queue.jsonl")
    ap.add_argument("--report-file", type=Path, default=REPO / "reports/subtitle-backfill-status.jsonl")
    ap.add_argument(
        "--state-file",
        type=Path,
        action="append",
        default=[],
        help="Upload state JSON. Can be passed multiple times.",
    )
    args = ap.parse_args()
    if not args.state_file:
        args.state_file = sorted((REPO / "state").glob("uploaded-shard-*.json"))

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    needs_login = not args.dry_run or args.lookup == "profile"
    if needs_login and (not email or not password):
        print("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required", file=sys.stderr)
        return 2

    session = login(email, password) if needs_login else None
    tasks = build_tasks(args, session)
    log(f"tasks={len(tasks)} dry_run={args.dry_run}")
    if args.dry_run:
        for row, upload, info in tasks:
            log(
                f"DRY episode_id={row.get('episode_id')} video_id={upload.get('prehrajto_video_id')} "
                f"detail={info.get('detail_url')} source={row.get('source_url')}"
            )
        return 0

    ok = fail = 0
    suffix_base = str(int(time.time()))
    for index, (row, upload, info) in enumerate(tasks, 1):
        video_id = int(upload["prehrajto_video_id"])
        detail_url = str(info["detail_url"])
        for subtitle_id in info.get("remove_subtitle_ids") or []:
            response = remove_subtitle(session, video_id, int(subtitle_id), int(info["page"]))
            log(f"remove stuck subtitle video_id={video_id} subtitle_id={subtitle_id} http={response.status_code}")
            time.sleep(0.3)
        source_url, track_url = source_with_subtitles(row)
        if not track_url:
            target_duration = None
            try:
                target_duration = resolve(detail_url, max_retries=1).duration_sec
            except Exception:
                pass
            source_url, track_url = find_alternate_track(row, target_duration, min_interval=args.search_min_interval)
        if not track_url:
            fail += 1
            append_jsonl(args.report_file, status_row(row, upload, "source_track_not_found", detail_url=detail_url))
            log(f"FAIL no subtitle track episode_id={row.get('episode_id')} video_id={video_id}")
            continue
        content = fetch_subtitle(track_url)
        ext, _mime = detect_subtitle_format(content)
        if ext == ".vtt":
            content = vtt_to_srt(content)
        elif ext != ".srt":
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(row, upload, "unsupported_subtitle_format", detail_url=detail_url, source_url=source_url, format=ext),
            )
            log(f"FAIL unsupported subtitle format ext={ext} episode_id={row.get('episode_id')}")
            continue
        suffix = f"{suffix_base}-{index}"
        response = upload_subtitle(session, video_id, int(info["page"]), content, suffix)
        log(
            f"POST {index}/{len(tasks)} episode_id={row.get('episode_id')} video_id={video_id} "
            f"http={response.status_code} source={source_url}"
        )
        if response.status_code != 200:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "upload_failed",
                    detail_url=detail_url,
                    source_url=source_url,
                    http_status=response.status_code,
                    response=response.text[:500],
                ),
            )
            continue
        if verify_tracks(detail_url, args.verify_timeout):
            ok += 1
            append_jsonl(args.report_file, status_row(row, upload, "uploaded", detail_url=detail_url, source_url=source_url))
            log(f"OK tracks verified episode_id={row.get('episode_id')} video_id={video_id}")
        else:
            fail += 1
            append_jsonl(args.report_file, status_row(row, upload, "verify_failed", detail_url=detail_url, source_url=source_url))
            log(f"FAIL tracks not verified episode_id={row.get('episode_id')} video_id={video_id}")
    log(f"done ok={ok} fail={fail}")
    return 0 if fail == 0 or args.allow_partial else 1


if __name__ == "__main__":
    raise SystemExit(main())
