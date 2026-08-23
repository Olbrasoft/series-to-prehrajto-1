#!/usr/bin/env python3
"""Prepare upload-ready source choices for series episodes.

This is a pre-upload planning step. It walks episode sources, checks language
signals for every candidate it sees, stores the evidence, and picks the best
source for later uploading.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_language_sources import audit_one, write_latest_index  # noqa: E402
from language_checks import metadata_language_hint, title_language_hint  # noqa: E402
from prehrajto_search import SearchResult, search_pages as search_prehrajto_pages  # noqa: E402
from source_quality import source_quality_score, source_quality_tier  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_UPLOAD_FILE_SIZE = 300 * 1024 * 1024
# Metadata collected during search can be optimistic. Use a planning margin;
# sync_batch still verifies the real stream against MIN_UPLOAD_FILE_SIZE.
MIN_PLANNED_FILE_SIZE = 350 * 1024 * 1024
FAILED_RETRY_AFTER = dt.timedelta(hours=24)
MAX_RESOLVABLE_CANDIDATE_PROBES = 1


def source_has_cz_audio_hint(source: dict) -> bool:
    normalized = {
        **source,
        "lang_class": source.get("lang_class") or source.get("db_lang_class"),
        "audio_lang": source.get("audio_lang") or source.get("db_audio_lang"),
    }
    return (metadata_language_hint(normalized) or title_language_hint(source.get("source_title") or source.get("title"))) in {
        "cz_audio_metadata",
        "cz_audio_lang_class",
        "cz_audio_title",
    }


def source_has_cz_subtitle_hint(source: dict) -> bool:
    normalized = {
        **source,
        "lang_class": source.get("lang_class") or source.get("db_lang_class"),
        "audio_lang": source.get("audio_lang") or source.get("db_audio_lang"),
    }
    return (metadata_language_hint(normalized) or title_language_hint(source.get("source_title") or source.get("title"))) in {
        "cz_subtitle_lang_class",
        "cz_subtitle_title",
    }


def source_has_upload_quality_hint(source: dict) -> bool:
    filesize = source.get("filesize_bytes")
    if filesize is not None and int(filesize) >= MIN_PLANNED_FILE_SIZE:
        return True
    return source_quality_score(source)[1] >= 1080


def source_precheck_score(source: dict) -> tuple[int, int, int, int]:
    cz_bonus = 1 if source_has_cz_audio_hint(source) else 0
    quality_bonus, resolution, filesize = source_quality_score(source)
    return cz_bonus, quality_bonus, resolution, filesize


def episode_preparation_score(episode: dict, burned: set[int] | None = None) -> tuple[int, int, int, int, int]:
    burned = burned or set()
    sources = [
        source
        for source in episode.get("sources") or []
        if source.get("source_id") is not None and int(source["source_id"]) not in burned
    ]
    if not sources:
        return (0, 0, 0, 0, 0)
    best_cz = max(
        (source_precheck_score(source) for source in sources if source_has_cz_audio_hint(source)),
        default=(0, 0, 0, 0),
    )
    best_subtitle = max(
        (source_precheck_score(source) for source in sources if source_has_cz_subtitle_hint(source)),
        default=(0, 0, 0, 0),
    )
    best_quality = max((source_quality_score(source) for source in sources), default=(0, 0, 0))
    return (
        1 if best_cz[1] > 0 else 0,
        1 if best_subtitle[1] > 0 else 0,
        int(best_quality[0]),
        int(best_quality[1]),
        len(sources),
    )


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def compact_plan_row(row: dict) -> dict:
    retained = []
    for source in row.get("tested_sources") or []:
        probe = (source.get("signals") or {}).get("provider_probe") or {}
        if probe.get("status") != "ok" or not probe.get("streams"):
            continue
        if source.get("verdict") not in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"}:
            continue
        retained.append(source)
    retained.sort(key=lambda source: tuple(source.get("score") or ()), reverse=True)
    return {**row, "tested_sources": retained[:4]}


def write_compacted_prepared(path: Path, new_rows: list[dict]) -> None:
    latest: dict[int, dict] = {}
    for row in load_jsonl(path) if path.exists() else []:
        if row.get("episode_id") is not None:
            latest[int(row["episode_id"])] = compact_plan_row(row)
    for row in new_rows:
        if row.get("episode_id") is not None:
            latest[int(row["episode_id"])] = compact_plan_row(row)
    rows = sorted(
        latest.values(),
        key=lambda item: (
            str(item.get("series_slug") or ""),
            int(item.get("season") or 0),
            int(item.get("episode") or 0),
            int(item.get("episode_id") or 0),
        ),
    )
    write_jsonl(path, rows)


def update_subtitle_followup_queue(path: Path, prepared_rows: list[dict]) -> None:
    latest: dict[int, dict] = {
        int(row["episode_id"]): row
        for row in load_jsonl(path)
        if row.get("episode_id") is not None
    }
    for row in prepared_rows:
        if row.get("episode_id") is None:
            continue
        episode_id = int(row["episode_id"])
        if not row.get("needs_subtitles_after_upload"):
            latest.pop(episode_id, None)
            continue
        selected = row.get("selected_source") or {}
        latest[episode_id] = {
            "created_at": row.get("prepared_at"),
            "episode_id": episode_id,
            "series_id": int(row["series_id"]),
            "series_slug": row.get("series_slug"),
            "series_title": row.get("series_title"),
            "season": int(row["season"]),
            "episode": int(row["episode"]),
            "episode_code": episode_code(row),
            "episode_name": row.get("episode_name"),
            "upload_display_suffix": "CZ Titulky",
            "source_id": selected.get("source_id"),
            "source_url": selected.get("source_url"),
            "source_title": selected.get("source_title"),
            "subtitle_status": "needs_subtitle_setup_after_prehrajto_processing",
            "reason": (
                "selected source needs Czech subtitles after Whisper detected non-Czech audio"
                if selected.get("verification_status") == "whisper_confirmed_non_cz_audio_needs_cz_subtitles"
                else "selected source has Czech subtitles but no Czech audio source was found"
            ),
        }
    write_jsonl(
        path,
        sorted(
            latest.values(),
            key=lambda item: (
                str(item.get("series_slug") or ""),
                int(item.get("season") or 0),
                int(item.get("episode") or 0),
                int(item.get("episode_id") or 0),
            ),
        ),
    )


def update_whisper_review_queue(path: Path, prepared_rows: list[dict]) -> None:
    latest: dict[tuple[int, int], dict] = {}
    for row in load_jsonl(path):
        if row.get("episode_id") is None or row.get("source_id") is None:
            continue
        latest[(int(row["episode_id"]), int(row["source_id"]))] = row

    for row in prepared_rows:
        for source in row.get("whisper_review_sources") or []:
            if source.get("source_id") is None:
                continue
            source_id = int(source["source_id"])
            latest[(int(row["episode_id"]), source_id)] = {
                "created_at": row.get("prepared_at"),
                "status": "needs_whisper",
                "reason": source.get("whisper_review_reason") or "quality candidate has no Czech title or metadata signal",
                "episode_id": int(row["episode_id"]),
                "series_id": int(row["series_id"]),
                "series_slug": row.get("series_slug"),
                "series_title": row.get("series_title"),
                "season": int(row["season"]),
                "episode": int(row["episode"]),
                "episode_code": episode_code(row),
                "episode_name": row.get("episode_name"),
                "provider": source.get("provider"),
                "source_id": source_id,
                "external_id": source.get("external_id"),
                "source_url": source.get("source_url"),
                "source_title": source.get("source_title"),
                "filesize_bytes": source.get("filesize_bytes"),
                "resolution_hint": source.get("resolution_hint"),
                "duration_sec": source.get("duration_sec"),
                "quality_tier": source.get("quality_tier"),
                "verdict": source.get("verdict"),
                "detected_by": source.get("detected_by"),
                "verification_status": source.get("verification_status"),
            }

    write_jsonl(
        path,
        sorted(
            latest.values(),
            key=lambda item: (
                str(item.get("series_slug") or ""),
                int(item.get("season") or 0),
                int(item.get("episode") or 0),
                -int(item.get("filesize_bytes") or 0),
                int(item.get("source_id") or 0),
            ),
        ),
    )


def burned_source_ids() -> set[int]:
    burned: set[int] = set()
    paths = [REPO_ROOT / "state" / "uploaded.json"]
    paths.extend(sorted((REPO_ROOT / "state").glob("uploaded-shard-*.json")))
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for failure in state.get("failed_attempts", []):
            if failure.get("permanent") and failure.get("source_id") is not None:
                burned.add(int(failure["source_id"]))
    return burned


def latest_prepared_rows(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    latest: dict[int, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                latest[int(row["episode_id"])] = row
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    return latest


def latest_usable_prepared_episode_ids(path: Path, burned: set[int]) -> set[int]:
    completed: set[int] = set()
    for episode_id, row in latest_prepared_rows(path).items():
        if not row.get("upload_ready"):
            continue
        selected = row.get("selected_source") or {}
        source_id = selected.get("source_id")
        if source_id is None or int(source_id) in burned:
            continue
        if not is_resolvable(selected):
            continue
        completed.add(episode_id)
    return completed


def retry_due(row: dict | None, *, now: dt.datetime) -> bool:
    if not row or not row.get("prepared_at"):
        return True
    selected = row.get("selected_source") or {}
    if selected and not is_resolvable(selected):
        return True
    try:
        prepared = dt.datetime.fromisoformat(str(row["prepared_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - prepared >= FAILED_RETRY_AFTER


def uploaded_episode_exclusions() -> tuple[set[int], set[tuple[int, int, int]]]:
    uploaded_ids: set[int] = set()
    uploaded_keys: set[tuple[int, int, int]] = set()
    paths = [REPO_ROOT / "state" / "uploaded.json"]
    paths.extend(sorted((REPO_ROOT / "state").glob("uploaded-shard-*.json")))
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for upload in state.get("uploads", []):
            if upload.get("episode_id") is not None:
                uploaded_ids.add(int(upload["episode_id"]))
            if all(upload.get(key) is not None for key in ("series_id", "season", "episode")):
                uploaded_keys.add(
                    (
                        int(upload["series_id"]),
                        int(upload["season"]),
                        int(upload["episode"]),
                    )
                )
    return uploaded_ids, uploaded_keys


def select_todo_shard(
    episodes: list[dict],
    *,
    shard_index: int,
    shard_count: int,
    limit: int,
) -> list[dict]:
    if shard_count < 1:
        raise ValueError("selection shard count must be at least 1")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("selection shard index must be between 0 and shard count - 1")
    return [
        episode
        for position, episode in enumerate(episodes)
        if position % shard_count == shard_index
    ][:limit]


def active_preparation_claims(path: Path, *, now: dt.datetime) -> list[dict]:
    active = []
    for row in load_jsonl(path):
        expires_at = row.get("expires_at")
        if not expires_at:
            continue
        try:
            expires = dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires > now and row.get("episode_id") is not None:
            active.append(row)
    return active


def claim_episode_batch(
    episodes: list[dict],
    *,
    path: Path,
    batch_id: str,
    shard_count: int,
    limit_per_shard: int,
    ttl_minutes: int,
    now: dt.datetime,
) -> list[dict]:
    if shard_count < 1:
        raise ValueError("claim shard count must be at least 1")
    if ttl_minutes < 1:
        raise ValueError("claim TTL must be at least 1 minute")
    existing = active_preparation_claims(path, now=now)
    claimed_ids = {int(row["episode_id"]) for row in existing}
    selected = [episode for episode in episodes if int(episode["episode_id"]) not in claimed_ids][
        : limit_per_shard * shard_count
    ]
    claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now + dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_claims = [
        {
            "episode_id": int(episode["episode_id"]),
            "series_id": int(episode["series_id"]),
            "series_slug": episode["series_slug"],
            "series_title": episode["series_title"],
            "season": int(episode["season"]),
            "episode": int(episode["episode"]),
            "claim_batch_id": batch_id,
            "claim_shard_index": position % shard_count,
            "claimed_at": claimed_at,
            "expires_at": expires_at,
            "status": "claimed",
        }
        for position, episode in enumerate(selected)
    ]
    write_jsonl(
        path,
        sorted(
            [*existing, *new_claims],
            key=lambda row: (
                str(row.get("claim_batch_id") or ""),
                int(row.get("claim_shard_index") or 0),
                int(row.get("episode_id") or 0),
            ),
        ),
    )
    return new_claims


def queued_episode_exclusions(path: Path) -> tuple[set[int], set[tuple[int, int, int]]]:
    queued_ids: set[int] = set()
    queued_keys: set[tuple[int, int, int]] = set()
    for row in load_jsonl(path):
        if row.get("episode_id") is not None:
            queued_ids.add(int(row["episode_id"]))
        if all(row.get(key) is not None for key in ("series_id", "season", "episode")):
            queued_keys.add((int(row["series_id"]), int(row["season"]), int(row["episode"])))
    return queued_ids, queued_keys


def backlog_candidate_to_queue_item(episode: dict, candidate: dict) -> dict:
    return {
        "series_id": episode["series_id"],
        "series_slug": episode["series_slug"],
        "series_title": episode["series_title"],
        "episode_id": episode["episode_id"],
        "season": episode["season"],
        "episode": episode["episode"],
        "episode_title": episode.get("episode_title"),
        "episode_name": episode.get("episode_name"),
        "episode_audio_langs": [],
        "episode_subtitle_langs": [],
        "provider": "prehrajto",
        "source_id": candidate["source_id"],
        "external_id": candidate.get("external_id"),
        "source_url": candidate.get("url"),
        "source_title": candidate.get("title"),
        "duration_sec": candidate.get("duration_sec"),
        "resolution_hint": candidate.get("resolution_hint"),
        "filesize_bytes": candidate.get("filesize_bytes"),
        "view_count": candidate.get("view_count"),
        "db_lang_class": candidate.get("lang_class"),
        "db_audio_lang": candidate.get("audio_lang"),
        "db_audio_confidence": candidate.get("audio_confidence"),
        "db_audio_detected_by": None,
        "source_origin": candidate.get("source_origin") or "production_db",
        "db_source_exists": candidate.get("db_source_exists", True),
        "quality_tier": candidate.get("quality_tier"),
        "metadata": {},
    }


def merge_backlog_sources(queue_rows: list[dict], backlog_path: Path) -> list[dict]:
    if not backlog_path.exists():
        return queue_rows
    backlog = load_jsonl(backlog_path)
    backlog_episode_ids = {int(episode["episode_id"]) for episode in backlog}
    queue_rows = [
        row
        for row in queue_rows
        if row.get("episode_id") is not None and int(row["episode_id"]) in backlog_episode_ids
    ]
    seen = {
        (int(row["episode_id"]), int(row["source_id"]))
        for row in queue_rows
        if row.get("episode_id") is not None and row.get("source_id") is not None
    }
    merged = list(queue_rows)
    for episode in backlog:
        for candidate in episode.get("candidates") or []:
            key = (int(episode["episode_id"]), int(candidate["source_id"]))
            if key in seen:
                continue
            url = candidate.get("url")
            if not url:
                continue
            merged.append(backlog_candidate_to_queue_item(episode, candidate))
            seen.add(key)
    return merged


def episode_code(episode: dict) -> str:
    return f"S{int(episode['season'] or 0):02d}E{int(episode['episode'] or 0):02d}"


def compact_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z0-9]", "", normalized.encode("ascii", "ignore").decode().lower())


def title_matches_episode(title: str, episode: dict) -> bool:
    compact = compact_title(title)
    season = int(episode["season"] or 0)
    number = int(episode["episode"] or 0)
    markers = [
        f"s{season:02d}e{number:02d}",
        f"s{season}e{number}",
        f"{season}x{number}",
        f"{season:02d}x{number:02d}",
    ]
    series_titles = [
        compact_title(value)
        for value in (episode.get("series_title"), episode.get("series_original_title"))
        if value
    ]
    for marker in markers:
        marker_at = compact.find(marker)
        if marker_at < 0:
            continue
        for series_title in series_titles:
            series_at = compact.find(series_title)
            if series_at < 0 or series_at > marker_at:
                continue
            between = compact[series_at + len(series_title) : marker_at]
            if not between:
                return True
    return False


def search_result_to_queue_item(result: SearchResult, episode: dict) -> dict:
    return {
        "series_id": episode["series_id"],
        "series_slug": episode["series_slug"],
        "series_title": episode["series_title"],
        "series_original_title": episode.get("series_original_title"),
        "episode_id": episode["episode_id"],
        "season": episode["season"],
        "episode": episode["episode"],
        "episode_title": episode.get("episode_title"),
        "episode_name": episode.get("episode_name"),
        "episode_audio_langs": [],
        "episode_subtitle_langs": [],
        "provider": "prehrajto",
        "source_id": result.source_id,
        "external_id": result.external_id,
        "source_url": result.url,
        "source_title": result.title,
        "duration_sec": result.duration_sec,
        "resolution_hint": result.resolution_hint,
        "filesize_bytes": result.filesize_bytes,
        "view_count": None,
        "db_lang_class": None,
        "db_audio_lang": None,
        "db_audio_confidence": None,
        "db_audio_detected_by": None,
        "source_origin": "prehrajto_search",
        "db_source_exists": False,
        "quality_tier": source_quality_tier(
            {
                "source_title": result.title,
                "resolution_hint": result.resolution_hint,
                "filesize_bytes": result.filesize_bytes,
            }
        ),
        "metadata": {},
    }


def usable_search_sources(results: list[SearchResult], episode: dict) -> list[dict]:
    sources = [
        search_result_to_queue_item(result, episode)
        for result in results
        if title_matches_episode(result.title, episode)
    ]
    return [
        source
        for source in sources
        if source_has_upload_quality_hint(source)
    ]


def live_search_candidates(episode: dict, *, limit: int, query_limit: int) -> list[dict]:
    titles = [
        title
        for title in dict.fromkeys(
            [episode.get("series_title"), episode.get("series_original_title")]
        )
        if title
    ]
    queries = [
        *(f"{title} {episode_code(episode)}" for title in titles),
        *(f"{title} {int(episode['season'])}x{int(episode['episode'])}" for title in titles),
    ]
    found: dict[str, dict] = {}
    for query in queries[: max(query_limit, 1)]:
        try:
            pages = search_prehrajto_pages(
                query,
                max_pages=2,
                should_fetch_next=lambda results: not usable_search_sources(results, episode),
            )
        except Exception as exc:
            print(f"Live search failed for {query!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for page in pages:
            for source in usable_search_sources(page, episode):
                found[source["external_id"]] = source
            if found:
                break
        if found:
            break
    return sorted(found.values(), key=source_precheck_score, reverse=True)[:limit]


def group_by_episode(rows: list[dict], backlog_path: Path) -> list[dict]:
    grouped: dict[int, dict] = {}
    for episode in load_jsonl(backlog_path):
        eid = int(episode["episode_id"])
        grouped[eid] = {
            "episode_id": eid,
            "series_id": episode["series_id"],
            "series_slug": episode["series_slug"],
            "series_title": episode["series_title"],
            "series_original_title": episode.get("series_original_title"),
            "imdb_votes": episode.get("imdb_votes") or 0,
            "imdb_rating": episode.get("imdb_rating") or 0,
            "csfd_rating": episode.get("csfd_rating") or 0,
            "season": episode["season"],
            "episode": episode["episode"],
            "episode_title": episode.get("episode_title"),
            "episode_name": episode.get("episode_name") or episode.get("episode_title"),
            "sources": [],
        }
    for row in rows:
        eid = int(row["episode_id"])
        if eid not in grouped:
            grouped[eid] = {
                "episode_id": eid,
                "series_id": row["series_id"],
                "series_slug": row["series_slug"],
                "series_title": row["series_title"],
                "series_original_title": row.get("series_original_title"),
                "imdb_votes": row.get("imdb_votes") or 0,
                "imdb_rating": row.get("imdb_rating") or 0,
                "csfd_rating": row.get("csfd_rating") or 0,
                "season": row["season"],
                "episode": row["episode"],
                "episode_name": row.get("episode_name") or row.get("episode_title"),
                "sources": [],
            }
        grouped[eid]["sources"].append(row)
    return sorted(
        grouped.values(),
        key=lambda item: (
            -int(item.get("imdb_votes") or 0),
            -float(item.get("imdb_rating") or 0),
            -float(item.get("csfd_rating") or 0),
            item["series_slug"],
            int(item["season"] or 0),
            int(item["episode"] or 0),
            item["episode_id"],
        ),
    )


def probe_resolution(result: dict, source: dict) -> int:
    probe = result.get("signals", {}).get("provider_probe") or {}
    streams = probe.get("streams") or []
    if streams:
        return max(int(stream.get("res") or 0) for stream in streams)
    return source_quality_score(source)[1]


def is_resolvable(result: dict) -> bool:
    probe = result.get("signals", {}).get("provider_probe") or {}
    return probe.get("status") == "ok" and bool(probe.get("streams"))


def source_score(result: dict, source: dict) -> tuple:
    verdict = result["verdict"]
    verdict_score = {
        "CZ_AUDIO": 100,
        "PROBABLE_CZ_AUDIO": 70,
        "CZ_SUBTITLES_ONLY": 35,
        "SK_AUDIO": 20,
        "UNKNOWN": 0,
        "NOT_CZ_AUDIO": -100,
    }.get(verdict, 0)
    detected_bonus = {"whisper": 30, "metadata": 20, "provider_tracks": 10, "title": 5}.get(result["detected_by"], 0)
    provider_bonus = {"prehrajto": 20, "sktorrent": 10, "sledujteto": 0}.get(source.get("provider"), 0)
    quality_bonus, quality_resolution, quality_filesize = source_quality_score(
        source,
        resolved_resolution=probe_resolution(result, source),
    )
    return (
        quality_bonus,
        quality_resolution,
        verdict_score + detected_bonus + provider_bonus,
        quality_filesize,
        int(source.get("view_count") or 0),
        -int(source["source_id"]),
    )


def prepare_episode(
    episode: dict,
    *,
    use_whisper: bool,
    sample_seconds: int,
    source_limit: int,
    burned: set[int],
    require_resolvable_source: bool,
    live_search: bool,
    live_search_limit: int,
    live_search_query_limit: int,
) -> dict:
    live_sources = [source for source in episode["sources"] if int(source["source_id"]) not in burned]
    if live_search:
        known_external_ids = {source.get("external_id") for source in live_sources}
        discovered = [
            source
            for source in live_search_candidates(
                episode,
                limit=live_search_limit,
                query_limit=live_search_query_limit,
            )
            if source.get("external_id") not in known_external_ids
        ]
        live_sources = discovered + live_sources
    prioritized = [
        source
        for source in live_sources
        if source_has_cz_audio_hint(source) and source_has_upload_quality_hint(source)
    ]
    subtitle_fallback = [
        source
        for source in live_sources
        if source not in prioritized
        and source_has_cz_subtitle_hint(source)
        and source_has_upload_quality_hint(source)
    ]
    whisper_candidates = [
        source
        for source in live_sources
        if source not in prioritized
        and source not in subtitle_fallback
        and source_has_upload_quality_hint(source)
    ]
    fallback = [
        source
        for source in live_sources
        if source not in prioritized
        and source not in subtitle_fallback
        and source not in whisper_candidates
    ]
    prioritized.sort(key=source_precheck_score, reverse=True)
    subtitle_fallback.sort(key=source_precheck_score, reverse=True)
    whisper_candidates.sort(key=source_precheck_score, reverse=True)
    fallback.sort(key=source_precheck_score, reverse=True)
    live_sources = prioritized + subtitle_fallback + whisper_candidates + fallback
    sources = live_sources[:source_limit] if source_limit > 0 else live_sources
    audited = []
    for source in sources:
        result = audit_one(
            source,
            use_whisper=False,
            sample_seconds=sample_seconds,
            probe_stream=False,
        )
        result["score"] = source_score(result, source)
        result["resolution_score"] = probe_resolution(result, source)
        result["quality_tier"] = source_quality_tier(source, resolved_resolution=result["resolution_score"])
        audited.append(result)

    sources_by_id = {int(source["source_id"]): source for source in sources}
    acceptable = [
        result
        for result in audited
        if result["verdict"] in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"}
        and (
            source_has_upload_quality_hint(sources_by_id[int(result["source_id"])])
            or result["resolution_score"] >= 1080
        )
    ]
    acceptable.sort(key=lambda result: tuple(result["score"]), reverse=True)
    subtitle_acceptable = [
        result
        for result in audited
        if result["verdict"] == "CZ_SUBTITLES_ONLY"
        and source_has_upload_quality_hint(sources_by_id[int(result["source_id"])])
    ]
    subtitle_acceptable.sort(key=lambda result: tuple(result["score"]), reverse=True)

    selected = None
    if require_resolvable_source or use_whisper:
        audited_by_id = {int(result["source_id"]): index for index, result in enumerate(audited)}
        verified_acceptable = []
        for preliminary in acceptable[:MAX_RESOLVABLE_CANDIDATE_PROBES]:
            source = sources_by_id[int(preliminary["source_id"])]
            verified = audit_one(
                source,
                use_whisper=use_whisper,
                sample_seconds=sample_seconds,
                probe_stream=True,
            )
            verified["score"] = source_score(verified, source)
            verified["resolution_score"] = probe_resolution(verified, source)
            verified["quality_tier"] = source_quality_tier(
                source,
                resolved_resolution=verified["resolution_score"],
            )
            audited[audited_by_id[int(verified["source_id"])]] = verified
            if not is_resolvable(verified):
                continue
            if verified["verdict"] not in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"}:
                continue
            verified_acceptable.append(verified)
            if len(verified_acceptable) >= 2:
                break
        if verified_acceptable:
            verified_acceptable.sort(key=lambda result: tuple(result["score"]), reverse=True)
            selected = verified_acceptable[0]
        if selected is None and subtitle_acceptable:
            verified_subtitles = []
            for preliminary in subtitle_acceptable[:MAX_RESOLVABLE_CANDIDATE_PROBES]:
                source = sources_by_id[int(preliminary["source_id"])]
                verified = audit_one(
                    source,
                    use_whisper=False,
                    sample_seconds=sample_seconds,
                    probe_stream=True,
                )
                verified["score"] = source_score(verified, source)
                verified["resolution_score"] = probe_resolution(verified, source)
                verified["quality_tier"] = source_quality_tier(
                    source,
                    resolved_resolution=verified["resolution_score"],
                )
                audited[audited_by_id[int(verified["source_id"])]] = verified
                if not is_resolvable(verified):
                    continue
                if verified["verdict"] != "CZ_SUBTITLES_ONLY":
                    continue
                verified_subtitles.append(verified)
                break
            if verified_subtitles:
                selected = verified_subtitles[0]
        if selected is None and use_whisper:
            whisper_acceptable = []
            whisper_subtitle_fallback = []
            unknown_quality = [
                result
                for result in audited
                if result["verdict"] not in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO", "CZ_SUBTITLES_ONLY"}
                and source_has_upload_quality_hint(sources_by_id[int(result["source_id"])])
            ]
            unknown_quality.sort(key=lambda result: tuple(result["score"]), reverse=True)
            for preliminary in unknown_quality[:MAX_RESOLVABLE_CANDIDATE_PROBES]:
                source = sources_by_id[int(preliminary["source_id"])]
                verified = audit_one(
                    source,
                    use_whisper=True,
                    sample_seconds=sample_seconds,
                    probe_stream=True,
                )
                verified["score"] = source_score(verified, source)
                verified["resolution_score"] = probe_resolution(verified, source)
                verified["quality_tier"] = source_quality_tier(
                    source,
                    resolved_resolution=verified["resolution_score"],
                )
                audited[audited_by_id[int(verified["source_id"])]] = verified
                if not is_resolvable(verified):
                    continue
                if verified["verdict"] == "CZ_AUDIO":
                    whisper_acceptable.append(verified)
                    break
                whisper = (verified.get("signals") or {}).get("whisper") or {}
                if whisper.get("status") == "ok":
                    subtitle_verified = {
                        **verified,
                        "original_verdict": verified.get("verdict"),
                        "verdict": "CZ_SUBTITLES_ONLY",
                        "verification_status": "whisper_confirmed_non_cz_audio_needs_cz_subtitles",
                    }
                    subtitle_verified["score"] = source_score(subtitle_verified, source)
                    whisper_subtitle_fallback.append(subtitle_verified)
            if whisper_acceptable:
                selected = whisper_acceptable[0]
            elif whisper_subtitle_fallback:
                whisper_subtitle_fallback.sort(key=lambda result: tuple(result["score"]), reverse=True)
                selected = whisper_subtitle_fallback[0]
    elif acceptable:
        selected = acceptable[0]
    elif subtitle_acceptable:
        selected = subtitle_acceptable[0]

    upload_ready = bool(selected and selected["verdict"] in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO", "CZ_SUBTITLES_ONLY"})
    needs_subtitles_after_upload = bool(selected and selected["verdict"] == "CZ_SUBTITLES_ONLY")
    selected_source_id = int(selected["source_id"]) if selected and selected.get("source_id") is not None else None
    whisper_review_sources = []
    if not use_whisper:
        for result in audited:
            if selected_source_id is not None and int(result["source_id"]) == selected_source_id:
                continue
            source = sources_by_id.get(int(result["source_id"]))
            if not source or not source_has_upload_quality_hint(source):
                continue
            if result["verdict"] in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO", "CZ_SUBTITLES_ONLY"}:
                continue
            whisper_review_sources.append(
                {
                    **result,
                    "whisper_review_reason": "quality candidate matches episode but has no Czech title or metadata signal, or has another language signal",
                }
            )
    return {
        "prepared_at": (
            audited[-1]["audited_at"]
            if audited
            else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "episode_id": episode["episode_id"],
        "series_id": episode["series_id"],
        "series_slug": episode["series_slug"],
        "series_title": episode["series_title"],
        "season": episode["season"],
        "episode": episode["episode"],
        "imdb_votes": episode.get("imdb_votes") or 0,
        "imdb_rating": episode.get("imdb_rating") or 0,
        "csfd_rating": episode.get("csfd_rating") or 0,
        "episode_name": episode.get("episode_name"),
        "tested_source_count": len(audited),
        "upload_ready": upload_ready,
        "upload_kind": "subtitles" if needs_subtitles_after_upload else ("audio" if selected else None),
        "needs_subtitles_after_upload": needs_subtitles_after_upload,
        "selected_source": selected,
        "tested_sources": audited,
        "whisper_review_sources": whisper_review_sources,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="backlog/language-audit-queue.jsonl.gz")
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--out", default="plans/prepared-episodes.jsonl")
    ap.add_argument("--audit-out", default="audits/language-audit.jsonl")
    ap.add_argument("--audit-latest-out", default="audits/language-audit-latest.jsonl.gz")
    ap.add_argument("--subtitle-followup-out", default="plans/subtitle-followup-queue.jsonl")
    ap.add_argument("--whisper-review-out", default="plans/whisper-review-queue.jsonl")
    ap.add_argument("--episode-limit", type=int, default=10)
    ap.add_argument("--selection-shard-index", type=int, default=0)
    ap.add_argument("--selection-shard-count", type=int, default=1)
    ap.add_argument("--claim-file", default="plans/preparation-claims.jsonl")
    ap.add_argument("--claim-batch-id")
    ap.add_argument("--claim-shard-index", type=int)
    ap.add_argument("--claim-shard-count", type=int, default=1)
    ap.add_argument("--claim-ttl-minutes", type=int, default=180)
    ap.add_argument("--claim-only", action="store_true")
    ap.add_argument("--source-limit-per-episode", type=int, default=8)
    ap.add_argument("--series-slug")
    ap.add_argument("--season", type=int)
    ap.add_argument("--episode", type=int)
    ap.add_argument("--use-whisper", action="store_true")
    ap.add_argument("--sample-seconds", type=int, default=45)
    ap.add_argument("--require-resolvable-source", action="store_true")
    ap.add_argument("--live-search", action="store_true")
    ap.add_argument("--live-search-limit", type=int, default=8)
    ap.add_argument("--live-search-query-limit", type=int, default=1)
    ap.add_argument("--upload-manifest", default="manifests/upload-ready.jsonl.gz")
    ap.add_argument("--include-upload-manifest", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    rows = merge_backlog_sources(load_jsonl(Path(args.queue)), Path(args.backlog))
    if args.series_slug:
        rows = [row for row in rows if row["series_slug"] == args.series_slug]
    episodes = group_by_episode(rows, Path(args.backlog))
    if args.series_slug:
        episodes = [episode for episode in episodes if episode["series_slug"] == args.series_slug]
    if args.season is not None:
        episodes = [episode for episode in episodes if int(episode["season"]) == args.season]
    if args.episode is not None:
        episodes = [episode for episode in episodes if int(episode["episode"]) == args.episode]
    burned = burned_source_ids()
    latest = latest_prepared_rows(Path(args.out))
    done = set() if args.refresh else latest_usable_prepared_episode_ids(Path(args.out), burned)
    uploaded_ids, uploaded_keys = uploaded_episode_exclusions()
    queued_ids, queued_keys = (
        (set(), set())
        if args.include_upload_manifest
        else queued_episode_exclusions(Path(args.upload_manifest))
    )
    now = dt.datetime.now(dt.timezone.utc)
    active_claims = active_preparation_claims(Path(args.claim_file), now=now)
    own_claims = [
        row
        for row in active_claims
        if args.claim_batch_id and row.get("claim_batch_id") == args.claim_batch_id
    ]
    claimed_elsewhere = {
        int(row["episode_id"])
        for row in active_claims
        if not args.claim_batch_id or row.get("claim_batch_id") != args.claim_batch_id
    }
    todo = [
        episode
        for episode in episodes
        if int(episode["episode_id"]) not in done
        and (args.refresh or retry_due(latest.get(int(episode["episode_id"])), now=now))
        and int(episode["episode_id"]) not in uploaded_ids
        and (
            int(episode["series_id"]),
            int(episode["season"]),
            int(episode["episode"]),
        )
        not in uploaded_keys
        and int(episode["episode_id"]) not in queued_ids
        and (
            int(episode["series_id"]),
            int(episode["season"]),
            int(episode["episode"]),
        )
        not in queued_keys
        and int(episode["episode_id"]) not in claimed_elsewhere
    ]
    todo.sort(
        key=lambda episode: (
            str((latest.get(int(episode["episode_id"])) or {}).get("prepared_at") or ""),
            tuple(-value for value in episode_preparation_score(episode, burned)),
            -int(episode.get("imdb_votes") or 0),
            -float(episode.get("imdb_rating") or 0),
            -float(episode.get("csfd_rating") or 0),
            int(episode.get("season") or 0),
            int(episode.get("episode") or 0),
            int(episode["episode_id"]),
        )
    )
    if args.claim_only:
        if not args.claim_batch_id:
            ap.error("--claim-only requires --claim-batch-id")
        claims = claim_episode_batch(
            todo,
            path=Path(args.claim_file),
            batch_id=args.claim_batch_id,
            shard_count=args.claim_shard_count,
            limit_per_shard=args.episode_limit,
            ttl_minutes=args.claim_ttl_minutes,
            now=now,
        )
        print(f"Claimed {len(claims)} episodes for batch {args.claim_batch_id}")
        return 0
    if args.claim_batch_id:
        claim_order = [
            int(row["episode_id"])
            for row in own_claims
            if args.claim_shard_index is None
            or int(row.get("claim_shard_index") or 0) == args.claim_shard_index
        ]
        todo_by_id = {int(episode["episode_id"]): episode for episode in todo}
        todo = [todo_by_id[episode_id] for episode_id in claim_order if episode_id in todo_by_id][
            : args.episode_limit
        ]
    else:
        todo = select_todo_shard(
            todo,
            shard_index=args.selection_shard_index,
            shard_count=args.selection_shard_count,
            limit=args.episode_limit,
        )

    prepared = [
        prepare_episode(
            episode,
            use_whisper=args.use_whisper,
            sample_seconds=args.sample_seconds,
            source_limit=args.source_limit_per_episode,
            burned=burned,
            require_resolvable_source=args.require_resolvable_source,
            live_search=args.live_search,
            live_search_limit=args.live_search_limit,
            live_search_query_limit=args.live_search_query_limit,
        )
        for episode in todo
    ]
    audit_rows = [source for episode in prepared for source in episode["tested_sources"]]
    write_compacted_prepared(Path(args.out), prepared)
    update_subtitle_followup_queue(Path(args.subtitle_followup_out), prepared)
    update_whisper_review_queue(Path(args.whisper_review_out), prepared)
    append_jsonl(Path(args.audit_out), audit_rows)
    write_latest_index(Path(args.audit_out), Path(args.audit_latest_out))

    for episode in prepared:
        selected = episode.get("selected_source")
        selected_text = (
            f"selected source_id={selected['source_id']} {selected['verdict']} by={selected['detected_by']}"
            if selected
            else "no acceptable source"
        )
        print(
            f"{episode['series_title']} S{int(episode['season']):02d}E{int(episode['episode']):02d}: "
            f"tested={episode['tested_source_count']} ready={episode['upload_ready']} {selected_text}"
        )
    print(f"Skipped {len(burned)} permanently failed source ids")
    print(f"Prepared {len(prepared)} episodes into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
