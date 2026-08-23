#!/usr/bin/env python3
"""Build a single upload-ready manifest from prepared series data."""

from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from description_quality import is_valid_generated_description

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_UPLOAD_FILE_SIZE = 300 * 1024 * 1024
# Search/provider metadata can overstate the final stream size. Keep a planning
# margin so upload workers do not waste batches on sources that HEAD later
# rejects as undersized.
MIN_PLANNED_FILE_SIZE = 350 * 1024 * 1024
TRANSIENT_FAILURE_COOLDOWN_SECONDS = int(os.environ.get("SYNC_TRANSIENT_FAILURE_COOLDOWN_SECONDS", "21600"))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_by_episode(rows: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in rows:
        if row.get("episode_id") is None:
            continue
        latest[int(row["episode_id"])] = row
    return latest


def episode_key(row: dict) -> tuple[int, int, int] | None:
    if row.get("series_id") is None or row.get("season") is None or row.get("episode") is None:
        return None
    return (int(row["series_id"]), int(row["season"]), int(row["episode"]))


def row_has_burned_source(row: dict, burned: set[int]) -> bool:
    return any(int(candidate["source_id"]) in burned for candidate in row.get("candidates") or [])


def merge_manifest(
    existing: list[dict],
    refreshed: list[dict],
    refreshed_episode_ids: set[int],
    *,
    uploaded_episode_ids: set[int],
    uploaded_episode_keys: set[tuple[int, int, int]],
    burned: set[int],
) -> list[dict]:
    rows_by_episode = {
        int(row["episode_id"]): row
        for row in existing
        if row.get("episode_id") is not None
        and int(row["episode_id"]) not in refreshed_episode_ids
        and int(row["episode_id"]) not in uploaded_episode_ids
        and episode_key(row) not in uploaded_episode_keys
        and not row_has_burned_source(row, burned)
    }
    for row in refreshed:
        rows_by_episode[int(row["episode_id"])] = row
    return list(rows_by_episode.values())


def latest_audits_by_source(rows: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in rows:
        if row.get("source_id") is None:
            continue
        source_id = int(row["source_id"])
        old = latest.get(source_id)
        if old is None or str(row.get("audited_at") or "") >= str(old.get("audited_at") or ""):
            latest[source_id] = row
    return latest


def failed_availability_urls(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    failed: set[str] = set()
    for row in load_jsonl(path):
        if row.get("ok") is False and row.get("source_url"):
            failed.add(str(row["source_url"]))
    return failed


def load_upload_state_exclusions() -> tuple[set[int], set[tuple[int, int, int]], set[int]]:
    uploaded_ids: set[int] = set()
    uploaded_keys: set[tuple[int, int, int]] = set()
    unavailable_sources: set[int] = set()
    now = datetime.now(timezone.utc)
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
            key = episode_key(upload)
            if key:
                uploaded_keys.add(key)
        for failure in state.get("failed_attempts", []):
            if failure.get("source_id") is None:
                continue
            if failure.get("permanent"):
                unavailable_sources.add(int(failure["source_id"]))
                continue
            failed_at = parse_failed_at(failure.get("failed_at"))
            if failed_at is None:
                continue
            age = (now - failed_at).total_seconds()
            if 0 <= age < TRANSIENT_FAILURE_COOLDOWN_SECONDS:
                unavailable_sources.add(int(failure["source_id"]))
    return uploaded_ids, uploaded_keys, unavailable_sources


def parse_failed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def description_indexes(rows: list[dict]) -> tuple[dict[int, dict], dict[int, dict]]:
    series: dict[int, dict] = {}
    episodes: dict[int, dict] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        if not is_valid_generated_description(row.get("generated_description") or ""):
            continue
        if row.get("kind") == "series" and row.get("series_id") is not None:
            series[int(row["series_id"])] = row
        elif row.get("kind") == "episode" and row.get("episode_id") is not None:
            episodes[int(row["episode_id"])] = row
    return series, episodes


def fallback_description_plan(episode: dict) -> dict | None:
    for key in ("description", "source_description", "series_description", "series_overview_en"):
        text = (episode.get(key) or "").strip()
        if text:
            return {
                "kind": "fallback",
                "generated_at": None,
                "model": "source-export",
                "source_hash": None,
                "generated_description": text,
            }
    series_title = str(episode.get("series_title") or "seriálu").strip()
    code = sxe(episode.get("season"), episode.get("episode"))
    subtitle = str(episode.get("episode_name") or episode.get("episode_title") or "").strip()
    text = f"Epizoda {code} seriálu {series_title}."
    if subtitle:
        text = f"{text[:-1]} s názvem {subtitle}."
    return {
        "kind": "temporary",
        "generated_at": None,
        "model": "deterministic-fallback",
        "source_hash": None,
        "generated_description": text,
    }


def sxe(season: int | None, episode: int | None) -> str:
    return f"S{int(season or 0):02d}E{int(episode or 0):02d}"


def display_name_from_plan(plan: dict) -> str:
    base = f"{plan['series_title']} {sxe(plan.get('season'), plan.get('episode'))}"
    subtitle = (plan.get("episode_name") or "").strip()
    if subtitle and subtitle.lower() != str(plan["series_title"]).lower():
        base = f"{base} - {subtitle}"
    suffix = "CZ Titulky" if plan.get("needs_subtitles_after_upload") or plan.get("upload_kind") == "subtitles" else "CZ Dabing"
    return f"{base} {suffix}"


def lang_class_from_verdict(verdict: str | None) -> str | None:
    if verdict in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"}:
        return "CZ_DUB"
    if verdict == "CZ_SUBTITLES_ONLY":
        return "CZ_SUB"
    return None


def audio_lang_from_verdict(verdict: str | None) -> str | None:
    return "cs" if verdict == "CZ_AUDIO" else None


def episode_from_prepared_plan(plan: dict) -> dict:
    selected = plan.get("selected_source") or {}
    candidate = {
        "source_id": selected.get("source_id"),
        "external_id": selected.get("external_id"),
        "url": selected.get("source_url"),
        "title": selected.get("source_title") or selected.get("provider_title"),
        "duration_sec": selected.get("duration_sec"),
        "resolution_hint": selected.get("resolution_hint"),
        "resolution_score": selected.get("resolution_score"),
        "filesize_bytes": selected.get("filesize_bytes"),
        "view_count": selected.get("view_count"),
        "lang_class": selected.get("db_lang_class") or lang_class_from_verdict(selected.get("verdict")),
        "audio_lang": selected.get("db_audio_lang") or audio_lang_from_verdict(selected.get("verdict")),
        "source_origin": selected.get("source_origin") or "prepared_source_plan",
        "db_source_exists": bool(selected.get("db_source_exists")),
        "quality_tier": selected.get("quality_tier"),
    }
    return {
        "episode_id": plan["episode_id"],
        "series_id": plan["series_id"],
        "series_slug": plan.get("series_slug"),
        "series_title": plan["series_title"],
        "series_original_title": plan.get("series_original_title"),
        "first_air_year": plan.get("first_air_year"),
        "season": plan["season"],
        "episode": plan["episode"],
        "episode_code": sxe(plan.get("season"), plan.get("episode")),
        "episode_title": None,
        "episode_name": plan.get("episode_name"),
        "air_date": plan.get("air_date"),
        "runtime": plan.get("runtime"),
        "imdb_id": plan.get("imdb_id"),
        "tmdb_id": plan.get("tmdb_id"),
        "imdb_rating": plan.get("imdb_rating"),
        "imdb_votes": plan.get("imdb_votes"),
        "csfd_rating": plan.get("csfd_rating"),
        "preferred_lang_class": candidate["lang_class"] or "CZ_DUB",
        "series_description": plan.get("series_description") or "",
        "series_overview_en": plan.get("series_overview_en") or "",
        "source_description": plan.get("source_description") or "",
        "description": plan.get("description") or "",
        "display_name": display_name_from_plan(plan),
        "candidates": [candidate] if candidate.get("source_id") and candidate.get("url") else [],
    }


def merge_backlog_with_prepared(backlog: list[dict], prepared: dict[int, dict]) -> list[dict]:
    rows_by_episode = {
        int(row["episode_id"]): row
        for row in backlog
        if row.get("episode_id") is not None
    }
    for episode_id, plan in prepared.items():
        if episode_id in rows_by_episode:
            continue
        if not plan.get("upload_ready") or not plan.get("selected_source"):
            continue
        rows_by_episode[episode_id] = episode_from_prepared_plan(plan)
    return list(rows_by_episode.values())


def upload_candidate_ids(plan: dict, burned: set[int]) -> list[int]:
    def passes(source):
        if source.get("verdict") not in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO", "CZ_SUBTITLES_ONLY"}:
            return False
        sid = int(source["source_id"])
        if sid in burned:
            return False
        fsize = source.get("filesize_bytes")
        if fsize is not None and fsize < MIN_PLANNED_FILE_SIZE:
            return False
        return True

    sources = [plan.get("selected_source"), *(plan.get("tested_sources") or [])]

    # Pass 1: only sources with valid provider_probe (resolvable)
    ids: list[int] = []
    seen: set[int] = set()
    for source in sources:
        if not source or not passes(source):
            continue
        provider_probe = (source.get("signals") or {}).get("provider_probe") or {}
        if provider_probe.get("status") != "ok":
            continue
        sid = int(source["source_id"])
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
    if ids:
        return ids

    # Pass 2: fallback – sources with valid URL and known filesize >= 300 MB
    for source in sources:
        if not source or not passes(source):
            continue
        fsize = source.get("filesize_bytes")
        if fsize is None or fsize < MIN_PLANNED_FILE_SIZE:
            continue
        if not source.get("source_url"):
            continue
        sid = int(source["source_id"])
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
    if ids:
        return ids

    # Pass 3: fallback – sources with valid URL (no probe/filesize available)
    for source in sources:
        if not source or not passes(source):
            continue
        if not source.get("source_url"):
            continue
        sid = int(source["source_id"])
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
    return ids


def upload_candidates(episode: dict, plan: dict, burned: set[int]) -> list[dict]:
    candidates_by_id = {int(candidate["source_id"]): candidate for candidate in episode.get("candidates") or []}
    for source in [plan.get("selected_source"), *(plan.get("tested_sources") or [])]:
        if not source or source.get("source_id") is None:
            continue
        source_id = int(source["source_id"])
        candidates_by_id.setdefault(
            source_id,
            {
                "source_id": source_id,
                "external_id": source.get("external_id"),
                "url": source.get("source_url"),
                "title": source.get("source_title") or source.get("provider_title"),
                "duration_sec": source.get("duration_sec"),
                "resolution_hint": source.get("resolution_hint"),
                "resolution_score": source.get("resolution_score"),
                "filesize_bytes": source.get("filesize_bytes"),
                "view_count": source.get("view_count"),
                "lang_class": source.get("db_lang_class") or lang_class_from_verdict(source.get("verdict")),
                "audio_lang": source.get("db_audio_lang") or audio_lang_from_verdict(source.get("verdict")),
                "source_origin": source.get("source_origin") or "production_db",
                "db_source_exists": bool(source.get("db_source_exists")),
                "quality_tier": source.get("quality_tier"),
            },
        )
    rows: list[dict] = []
    for source_id in upload_candidate_ids(plan, burned):
        candidate = candidates_by_id.get(source_id)
        if candidate and candidate.get("url"):
            rows.append(candidate)
    return rows


def build_manifest(
    *,
    backlog_path: Path,
    prepared_path: Path,
    descriptions_path: Path,
    audits_path: Path,
    limit: int,
    require_episode_description: bool,
    require_whisper: bool,
    failed_availability: set[str] | None = None,
) -> tuple[list[dict], Counter]:
    prepared = latest_by_episode(load_jsonl(prepared_path))
    backlog = merge_backlog_with_prepared(load_jsonl(backlog_path), prepared)
    desc_series, desc_episodes = description_indexes(load_jsonl(descriptions_path))
    audits = latest_audits_by_source(load_jsonl(audits_path))
    uploaded_episode_ids, uploaded_episode_keys, burned = load_upload_state_exclusions()
    rows: list[dict] = []
    queued_episode_keys: set[tuple[int, int, int]] = set()
    stats: Counter = Counter()
    failed_availability = failed_availability or set()

    for episode in backlog:
        if limit and len(rows) >= limit:
            break
        episode_id = int(episode["episode_id"])
        if episode_id in uploaded_episode_ids:
            stats["already_uploaded_episode_id"] += 1
            continue
        if episode_key(episode) in uploaded_episode_keys:
            stats["already_uploaded_episode_key"] += 1
            continue
        key = episode_key(episode)
        if key in queued_episode_keys:
            stats["duplicate_episode_key"] += 1
            continue
        plan = prepared.get(episode_id)
        if not plan:
            stats["missing_source_plan"] += 1
            continue
        selected = plan.get("selected_source") or {}
        if not plan.get("upload_ready") or not selected:
            stats["not_upload_ready"] += 1
            continue
        source_id = int(selected["source_id"])
        if source_id in burned:
            stats["selected_source_burned"] += 1
            continue
        fsize = selected.get("filesize_bytes")
        if fsize is not None and fsize < MIN_PLANNED_FILE_SIZE:
            stats["selected_source_undersize"] += 1
            continue
        resolvable = bool(((selected.get("signals") or {}).get("provider_probe") or {}).get("streams"))
        if fsize is None and not resolvable:
            stats["selected_source_no_size_info"] += 1
            continue
        candidates = upload_candidates(episode, plan, burned)
        if failed_availability:
            before = len(candidates)
            candidates = [candidate for candidate in candidates if candidate.get("url") not in failed_availability]
            stats["github_availability_failed"] += before - len(candidates)
        if not candidates:
            stats["selected_source_not_in_backlog"] += 1
            continue
        candidate = candidates[0]
        audit = audits.get(source_id) or selected
        whisper = (audit.get("signals") or {}).get("whisper") or {}
        if require_whisper and whisper.get("status") != "ok":
            stats["missing_whisper_confirmation"] += 1
            continue

        ep_desc = desc_episodes.get(episode_id)
        series_desc = desc_series.get(int(episode["series_id"]))
        description_plan = ep_desc or series_desc or fallback_description_plan(episode)
        if not description_plan:
            stats["missing_description"] += 1
            continue
        if require_episode_description and not ep_desc:
            stats["missing_episode_description"] += 1
            continue
        display_name = display_name_from_plan(plan)

        manifest_row = {
            **episode,
            "display_name": display_name,
            "candidates": candidates,
            "upload_manifest": {
                "schema_version": 1,
                "upload_job": {
                    "series_id": episode.get("series_id"),
                    "series_title": episode.get("series_title"),
                    "season": episode.get("season"),
                    "episode": episode.get("episode"),
                    "episode_id": episode_id,
                    "display_name": display_name,
                    "description": description_plan.get("generated_description"),
                    "source_id": source_id,
                    "source_url": candidate.get("url"),
                    "source_title": candidate.get("title"),
                    "external_id": candidate.get("external_id"),
                    "lang_class": candidate.get("lang_class"),
                    "resolution_hint": candidate.get("resolution_hint"),
                    "upload_kind": plan.get("upload_kind") or ("subtitles" if selected.get("verdict") == "CZ_SUBTITLES_ONLY" else "audio"),
                    "needs_subtitles_after_upload": bool(plan.get("needs_subtitles_after_upload")),
                },
                "source_plan": {
                    "prepared_at": plan.get("prepared_at"),
                    "tested_source_count": plan.get("tested_source_count"),
                    "selected_source_id": source_id,
                    "source_url": candidate.get("url"),
                    "source_title": candidate.get("title"),
                    "verdict": selected.get("verdict"),
                    "detected_by": selected.get("detected_by"),
                    "confidence": selected.get("confidence"),
                    "resolution_score": selected.get("resolution_score"),
                    "verification_status": selected.get("verification_status"),
                    "cz_audio_verified": selected.get("cz_audio_verified"),
                    "resolvable": bool(((selected.get("signals") or {}).get("provider_probe") or {}).get("streams")),
                    "upload_kind": plan.get("upload_kind") or ("subtitles" if selected.get("verdict") == "CZ_SUBTITLES_ONLY" else "audio"),
                    "needs_subtitles_after_upload": bool(plan.get("needs_subtitles_after_upload")),
                },
                "language_audit": {
                    "audited_at": audit.get("audited_at"),
                    "source_id": source_id,
                    "verdict": audit.get("verdict"),
                    "detected_by": audit.get("detected_by"),
                    "confidence": audit.get("confidence"),
                    "verification_status": audit.get("verification_status"),
                    "cz_audio_verified": audit.get("cz_audio_verified"),
                    "whisper": whisper,
                    "metadata_audio_lang": (audit.get("signals") or {}).get("metadata_audio_lang"),
                    "title_lang_class": (audit.get("signals") or {}).get("title_lang_class"),
                },
                "description": {
                    "kind": description_plan.get("kind"),
                    "generated_at": description_plan.get("generated_at"),
                    "model": description_plan.get("model"),
                    "source_hash": description_plan.get("source_hash"),
                    "text": description_plan.get("generated_description"),
                },
            },
        }
        rows.append(manifest_row)
        if key:
            queued_episode_keys.add(key)
        stats["ready"] += 1
        stats[f"description_{description_plan.get('kind')}"] += 1
        stats[f"language_{selected.get('detected_by') or 'unknown'}"] += 1

    return rows, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--prepared", default="plans/prepared-episodes.jsonl")
    ap.add_argument("--descriptions", default="plans/descriptions.jsonl")
    ap.add_argument("--audits", default="audits/language-audit-latest.jsonl.gz")
    ap.add_argument("--out", default="manifests/upload-ready.jsonl.gz")
    ap.add_argument("--report", default="reports/upload-manifest.json")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--require-episode-description", action="store_true")
    ap.add_argument("--require-whisper", action="store_true")
    ap.add_argument("--availability-report", default="reports/source-availability.jsonl")
    args = ap.parse_args()

    rows, stats = build_manifest(
        backlog_path=REPO_ROOT / args.backlog,
        prepared_path=REPO_ROOT / args.prepared,
        descriptions_path=REPO_ROOT / args.descriptions,
        audits_path=REPO_ROOT / args.audits,
        limit=args.limit,
        require_episode_description=args.require_episode_description,
        require_whisper=args.require_whisper,
        failed_availability=failed_availability_urls(REPO_ROOT / args.availability_report),
    )
    output_path = REPO_ROOT / args.out
    refreshed_episode_ids = {
        int(row["episode_id"])
        for row in rows
        if row.get("episode_id") is not None
    }
    uploaded_episode_ids, uploaded_episode_keys, burned = load_upload_state_exclusions()
    merged_rows = merge_manifest(
        load_jsonl(output_path),
        rows,
        refreshed_episode_ids,
        uploaded_episode_ids=uploaded_episode_ids,
        uploaded_episode_keys=uploaded_episode_keys,
        burned=burned,
    )
    write_jsonl(output_path, merged_rows)
    report = {
        "count": len(merged_rows),
        "new_count": len(rows),
        "retained_count": len(merged_rows) - len(rows),
        "stats": dict(stats),
    }
    report_path = REPO_ROOT / args.report
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
