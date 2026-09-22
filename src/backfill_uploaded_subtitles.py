#!/usr/bin/env python3
"""Attach Czech subtitles to already uploaded subtitle-only episodes.

Prehraj.to accepts SRT uploads, then serves processed subtitles as VTT tracks.
A controlled WEBVTT upload stayed in "Zpracovává se" for over ten minutes;
the same content worked after conversion to SRT. Provider VTT is therefore
converted to strict CRLF SRT with a unique short filename.
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
from collections.abc import Iterator

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prehrajto_search import search_pages  # noqa: E402
from prehrajto_upload import ACCEPT_LANG, SEC_CH_UA, login  # noqa: E402
from resolve_stream import ResolveError, ResolvedUpload, resolve  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROFILE_URL = "https://prehraj.to/profil/nahrana-videa"
DETAIL_BASE = "https://prehraj.to"
SAFE_LANGS = {"cs", "cz", "cze", "ces", "česky", "cesky"}
TERMINAL_STATUSES = {
    "already_has_tracks",
    "invalid_subtitle_format",
    "source_track_not_found",
    "uploaded",
    "unsupported_subtitle_format",
}
SUBMITTED_STATUSES = {"submission_pending", "submitted", "submission_unknown", "subtitle_processing"}
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
            episode_id = int(row["episode_id"])
            previous = latest.get(episode_id)
            if previous is None or str(row.get("checked_at") or "") >= str(previous.get("checked_at") or ""):
                latest[episode_id] = row
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


def load_uploads(paths: list[Path], upload_account: str | None = None) -> dict[int, dict]:
    by_episode: dict[int, dict] = {}
    for path in paths:
        state = load_json(path)
        for upload in state.get("uploads", []):
            if upload_account and upload.get("upload_account") != upload_account:
                continue
            try:
                episode_id = int(upload["episode_id"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(upload.get("display_name") or "")
            if not re.search(r"\bCZ\s+titulky\b", name, re.IGNORECASE):
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
    if not out:
        raise ValueError("subtitle contains no timed cues")
    return ("\r\n\r\n".join(out) + "\r\n").encode("utf-8")


def normalize_srt(srt_bytes: bytes) -> bytes:
    text = srt_bytes.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", text)
    timestamp_re = re.compile(r"^(\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}).*$")
    out: list[str] = []
    number = 1
    for block in re.split(r"\n\s*\n", text):
        lines = [line.rstrip() for line in block.strip().split("\n") if line.strip()]
        if lines and lines[0].isdigit():
            lines = lines[1:]
        if not lines:
            continue
        match = timestamp_re.match(lines[0])
        if not match:
            continue
        lines[0] = match.group(1)
        out.append(f"{number}\r\n" + "\r\n".join(lines))
        number += 1
    if not out:
        raise ValueError("subtitle contains no valid SRT cues")
    return ("\r\n\r\n".join(out) + "\r\n").encode("utf-8")


def pick_czech_track(resolved: ResolvedUpload) -> str | None:
    for track in resolved.tracks:
        if (track.lang or "").strip().lower() in SAFE_LANGS:
            return track.url
    return None


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


def find_profile_detail(session: requests.Session, upload: dict) -> dict | None:
    """Search this account's listing once, then match the immutable video ID."""
    response = session.get(
        PROFILE_URL, params={"searchPhrase": upload["display_name"]},
        headers={"Referer": PROFILE_URL}, timeout=30,
    )
    response.raise_for_status()
    return extract_blocks(response.text, 1).get(int(upload["prehrajto_video_id"]))


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
            if pick_czech_track(resolved):
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
        "upload_account": upload.get("upload_account"),
        "prehrajto_video_id": upload.get("prehrajto_video_id"),
    }
    out.update(extra)
    return out


def newest_upload_first(item: tuple[dict, dict]) -> tuple[str, int]:
    row, upload = item
    return (
        str(upload.get("uploaded_at") or ""),
        int(row.get("episode_id") or 0),
    )


def pending_uploads(
    followup_file: Path,
    state_files: list[Path],
    report_file: Path,
    *,
    upload_account: str | None = None,
    retry_reported: bool = False,
    episode_ids: list[int] | None = None,
) -> list[tuple[dict, dict]]:
    followups = {int(row["episode_id"]): row for row in load_jsonl(followup_file)}
    uploads = load_uploads(state_files, upload_account)
    latest_status = load_latest_status(report_file)
    matched = []
    for episode_id, upload in uploads.items():
        if episode_ids and episode_id not in episode_ids:
            continue
        followup = followups.get(episode_id, {})
        if not row_pending(followup):
            continue
        previous = latest_status.get(episode_id)
        if previous and previous.get("status") in TERMINAL_STATUSES and not retry_reported:
            continue
        # Uploaded video metadata is authoritative, even if its follow-up row
        # is missing or still references a previously considered source.
        row = {**followup, **{key: value for key, value in upload.items() if value is not None}}
        matched.append((row, upload))
    return matched


def iter_tasks(args: argparse.Namespace, session: requests.Session | None) -> Iterator[tuple[dict, dict, dict]]:
    latest_status = load_latest_status(args.report_file)
    matched = pending_uploads(
        args.followup_file, args.state_file, args.report_file,
        upload_account=getattr(args, "upload_account", None),
        retry_reported=args.retry_reported, episode_ids=args.episode_id,
    )
    matched.sort(key=newest_upload_first, reverse=True)
    # Inspect unseen uploads first, newest first. Rotate retries by their last
    # check so unavailable targets cannot consume every bounded batch forever.
    matched.sort(
        key=lambda item: str(
            latest_status.get(int(item[0]["episode_id"]), {}).get("checked_at") or ""
        )
    )
    matched = [item for item in matched if latest_status.get(int(item[0]["episode_id"]), {}).get("status") not in SUBMITTED_STATUSES]
    # Reserve one slot for deferred discovery, while keeping the remaining
    # batch available to uploads whose original source already has subtitles.
    deferred = next((item for item in matched if latest_status.get(int(item[0]["episode_id"]), {}).get("status") == "source_search_pending"), None)
    if deferred is not None:
        matched.remove(deferred)
        matched.insert(0, deferred)
    started = time.monotonic()
    selected = 0
    for inspected, (row, upload) in enumerate(matched, 1):
        if time.monotonic() >= getattr(args, "deadline", float("inf")) or (getattr(args, "max_runtime", 0) and time.monotonic() - started >= args.max_runtime):
            log("stop batch runtime budget; checkpoint before continuing")
            break
        if args.max_rows and inspected > args.max_rows:
            log(f"stop max_rows={args.max_rows} inspected={inspected - 1}")
            break
        video_id = int(upload["prehrajto_video_id"])
        previous = latest_status.get(int(row["episode_id"]), {})
        # Submission retries are verification-only. A lost HTTP response must
        # never cause another POST while the server may still be processing it.
        if previous.get("status") in SUBMITTED_STATUSES:
            continue
        if previous.get("detail_url") and str(previous.get("prehrajto_video_id")) == str(video_id):
            info = {"page": 1, "processing": False, "detail_url": previous["detail_url"]}
        elif args.lookup == "profile-search":
            if session is None:
                raise RuntimeError("profile search requires a logged-in session")
            try:
                info = find_profile_detail(session, upload)
            except requests.RequestException as exc:
                log(f"profile search failed video_id={video_id}: {exc}")
                info = None
        elif args.lookup == "profile":
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
        if pick_czech_track(current):
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "already_has_tracks",
                    detail_url=detail_url,
                    track_count=len(current.tracks),
                    subtitle_language="cs",
                ),
            )
            log(f"skip already has Czech tracks video_id={video_id} tracks={len(current.tracks)}")
            continue
        info["resolved"] = current
        yield row, upload, info
        selected += 1
        if args.limit and selected >= args.limit:
            break


def build_tasks(args: argparse.Namespace, session: requests.Session | None) -> list[tuple[dict, dict, dict]]:
    return list(iter_tasks(args, session))


def verify_submissions(args: argparse.Namespace) -> None:
    """Check a bounded set once, without sleeping or re-uploading."""
    previous_rows = load_latest_status(args.report_file).values()
    pending = [row for row in previous_rows if row.get("status") in SUBMITTED_STATUSES
               and (not args.upload_account or row.get("upload_account") == args.upload_account)
               and (not args.episode_id or row.get("episode_id") in args.episode_id)]
    pending.sort(key=lambda row: str(row.get("checked_at") or ""))
    deadline = min(time.monotonic() + 120, getattr(args, "deadline", float("inf")))
    for row in pending[:args.verification_limit]:
        if time.monotonic() >= deadline:
            break
        try:
            verified = verify_tracks(row["detail_url"], 0)
        except Exception as exc:
            log(f"verification deferred episode_id={row['episode_id']}: {exc}")
            continue
        status = "uploaded" if verified else "subtitle_processing"
        append_jsonl(args.report_file, {**row, "status": status, "checked_at": now_iso()})
        log(f"verify episode_id={row['episode_id']} status={status}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload-account", choices=["primary", "serialy"])
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--max-runtime", type=int, default=0, help="Stop selecting work after this many seconds")
    ap.add_argument("--defer-verification", action="store_true")
    ap.add_argument("--verification-limit", type=int, default=100)
    ap.add_argument("--alternate-limit", type=int, default=0, help="Maximum slow alternate searches per batch; zero is unlimited")
    ap.add_argument("--max-profile-pages", type=int, default=40)
    ap.add_argument("--verify-timeout", type=int, default=70)
    ap.add_argument("--search-min-interval", type=float, default=10.0)
    ap.add_argument("--lookup", choices=["public", "profile", "profile-search"], default="public")
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
    needs_login = not args.dry_run or args.lookup in {"profile", "profile-search"}
    if needs_login and (not email or not password):
        print("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required", file=sys.stderr)
        return 2

    session = login(email, password) if needs_login else None
    args.deadline = time.monotonic() + args.max_runtime if args.max_runtime else float("inf")
    if not args.dry_run:
        verify_submissions(args)
    tasks = iter_tasks(args, session)
    if args.dry_run:
        for row, upload, info in tasks:
            log(
                f"DRY episode_id={row.get('episode_id')} video_id={upload.get('prehrajto_video_id')} "
                f"detail={info.get('detail_url')} source={row.get('source_url')}"
            )
        return 0

    ok = fail = 0
    alternate_searches = 0
    # Keep names below the portal's label truncation threshold, including
    # two-digit batch indices. Pending submissions are never sent twice.
    suffix_base = str(int(time.time()))[-6:]
    for index, (row, upload, info) in enumerate(tasks, 1):
        video_id = int(upload["prehrajto_video_id"])
        detail_url = str(info["detail_url"])
        # Preserve existing tracks, including attachments still being processed.
        if info.get("subtitle_count", 0) and not info["resolved"].tracks:
            append_jsonl(args.report_file, status_row(row, upload, "target_processing", detail_url=detail_url))
            continue
        source_url, track_url = source_with_subtitles(row)
        if not track_url:
            if args.alternate_limit and alternate_searches >= args.alternate_limit:
                append_jsonl(args.report_file, status_row(row, upload, "source_search_pending", detail_url=detail_url))
                continue
            alternate_searches += 1
            target_duration = info["resolved"].duration_sec
            source_url, track_url = find_alternate_track(row, target_duration, min_interval=args.search_min_interval)
        if not track_url:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "source_track_not_found",
                    detail_url=detail_url,
                    source_url=row.get("source_url"),
                ),
            )
            log(f"FAIL no subtitle track episode_id={row.get('episode_id')} video_id={video_id}")
            continue
        try:
            content = fetch_subtitle(track_url)
        except Exception as exc:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "subtitle_fetch_failed",
                    detail_url=detail_url,
                    source_url=source_url,
                    reason=str(exc),
                ),
            )
            log(f"FAIL subtitle fetch episode_id={row.get('episode_id')} {exc}")
            continue
        ext, _mime = detect_subtitle_format(content)
        if ext not in {".vtt", ".srt"}:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(row, upload, "unsupported_subtitle_format", detail_url=detail_url, source_url=source_url, format=ext),
            )
            log(f"FAIL unsupported subtitle format ext={ext} episode_id={row.get('episode_id')}")
            continue
        try:
            content = vtt_to_srt(content) if ext == ".vtt" else normalize_srt(content)
        except ValueError as exc:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "invalid_subtitle_format",
                    detail_url=detail_url,
                    source_url=source_url,
                    source_format=ext,
                    reason=str(exc),
                ),
            )
            log(f"FAIL invalid subtitle episode_id={row.get('episode_id')} {exc}")
            continue
        suffix = f"{suffix_base}-{index}"
        submitted = status_row(row, upload, "submission_pending", detail_url=detail_url,
                               source_url=source_url, source_format=ext, uploaded_format="srt",
                               subtitle_language="cs", submitted_at=now_iso())
        append_jsonl(args.report_file, submitted)
        try:
            response = upload_subtitle(session, video_id, int(info["page"]), content, suffix)
        except requests.RequestException as exc:
            append_jsonl(args.report_file, {**submitted, "status": "submission_unknown", "reason": str(exc)})
            log(f"submission outcome unknown episode_id={row.get('episode_id')}: {exc}")
            continue
        log(
            f"POST {index} episode_id={row.get('episode_id')} video_id={video_id} "
            f"http={response.status_code} source={source_url}"
        )
        if response.status_code != 200:
            fail += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "submission_unknown" if response.status_code >= 500 else "upload_failed",
                    detail_url=detail_url,
                    source_url=source_url,
                    http_status=response.status_code,
                    response=response.text[:500],
                ),
            )
            continue
        append_jsonl(args.report_file, {**submitted, "status": "submitted", "checked_at": now_iso()})
        if args.defer_verification:
            log(f"submitted; verify next batch episode_id={row.get('episode_id')}")
            continue
        if verify_tracks(detail_url, args.verify_timeout):
            ok += 1
            append_jsonl(
                args.report_file,
                status_row(
                    row,
                    upload,
                    "uploaded",
                    detail_url=detail_url,
                    source_url=source_url,
                    source_format=ext,
                    uploaded_format="srt",
                    subtitle_language="cs",
                ),
            )
            log(f"OK tracks verified episode_id={row.get('episode_id')} video_id={video_id}")
        else:
            fail += 1
            append_jsonl(args.report_file, {**submitted, "status": "subtitle_processing", "checked_at": now_iso()})
            log(f"PENDING tracks not yet ready episode_id={row.get('episode_id')} video_id={video_id}")
    log(f"done ok={ok} fail={fail}")
    return 0 if fail == 0 or args.allow_partial else 1


if __name__ == "__main__":
    raise SystemExit(main())
