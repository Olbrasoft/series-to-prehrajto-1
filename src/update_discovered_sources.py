#!/usr/bin/env python3
"""Build an import-ready catalog of discovered episode sources.

The catalog is intentionally derived from repo state so source discovery can be
imported back into production later without re-querying production DB.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

from source_quality import source_quality_tier

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_uploaded_state() -> tuple[set[int], set[int]]:
    uploaded: set[int] = set()
    dead: set[int] = set()
    paths = [REPO_ROOT / "state" / "uploaded.json"]
    paths.extend(sorted((REPO_ROOT / "state").glob("uploaded-shard-*.json")))
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for row in state.get("uploads", []):
            if row.get("source_id") is not None:
                uploaded.add(int(row["source_id"]))
        for row in state.get("failed_attempts", []):
            if row.get("permanent") and row.get("source_id") is not None:
                dead.add(int(row["source_id"]))
    return uploaded, dead


def latest_audits(path: Path) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for row in load_jsonl(path):
        if row.get("source_id") is None:
            continue
        source_id = int(row["source_id"])
        old = latest.get(source_id)
        if old is None or str(row.get("audited_at") or "") >= str(old.get("audited_at") or ""):
            latest[source_id] = row
    return latest


def source_key(provider: str, external_id: str | None, url: str | None) -> str:
    if external_id:
        return f"{provider}:external:{external_id}"
    return f"{provider}:url:{url or ''}"


def catalog_row(
    episode: dict[str, Any],
    candidate: dict[str, Any],
    *,
    audit: dict[str, Any] | None,
    uploaded: set[int],
    dead: set[int],
) -> dict[str, Any]:
    source_id = int(candidate["source_id"])
    provider = candidate.get("provider") or "prehrajto"
    verdict = (audit or {}).get("verdict")
    detected_by = (audit or {}).get("detected_by")
    signals = (audit or {}).get("signals") or {}
    status = "rejected"
    if source_id in dead:
        status = "dead"
    elif source_id in uploaded:
        status = "uploaded"
    elif verdict in {"CZ_AUDIO", "PROBABLE_CZ_AUDIO"} or candidate.get("lang_class") in {"CZ_DUB", "CZ_NATIVE"}:
        status = source_quality_tier(candidate)

    return {
        "schema_version": 1,
        "source_key": source_key(provider, candidate.get("external_id"), candidate.get("url")),
        "provider": provider,
        "source_id": source_id,
        "external_id": candidate.get("external_id"),
        "source_url": candidate.get("url"),
        "source_title": candidate.get("title"),
        "source_origin": candidate.get("source_origin") or "production_db",
        "db_source_exists": bool(candidate.get("db_source_exists", True)),
        "series_id": episode.get("series_id"),
        "series_slug": episode.get("series_slug"),
        "series_title": episode.get("series_title"),
        "episode_id": episode.get("episode_id"),
        "season": episode.get("season"),
        "episode": episode.get("episode"),
        "episode_title": episode.get("episode_title"),
        "episode_name": episode.get("episode_name"),
        "duration_sec": candidate.get("duration_sec"),
        "resolution_hint": candidate.get("resolution_hint"),
        "resolution_score": candidate.get("resolution_score"),
        "filesize_bytes": candidate.get("filesize_bytes"),
        "view_count": candidate.get("view_count"),
        "quality_tier": source_quality_tier(candidate),
        "status": status,
        "import_status": "already_in_db" if candidate.get("db_source_exists", True) else "new_for_db",
        "db_lang_class": candidate.get("lang_class"),
        "db_audio_lang": candidate.get("audio_lang"),
        "language_verdict": verdict,
        "language_detected_by": detected_by,
        "language_confidence": (audit or {}).get("confidence"),
        "verification_status": (audit or {}).get("verification_status"),
        "metadata_audio_lang": signals.get("metadata_audio_lang"),
        "title_lang_class": signals.get("title_lang_class"),
        "whisper": signals.get("whisper"),
    }


def candidate_from_audit(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": audit.get("provider") or "prehrajto",
        "source_id": audit.get("source_id"),
        "external_id": audit.get("external_id"),
        "url": audit.get("source_url"),
        "title": audit.get("source_title") or audit.get("provider_title"),
        "duration_sec": audit.get("duration_sec"),
        "resolution_hint": audit.get("resolution_hint"),
        "resolution_score": audit.get("resolution_score"),
        "filesize_bytes": audit.get("filesize_bytes"),
        "view_count": audit.get("view_count"),
        "quality_tier": audit.get("quality_tier"),
        "source_origin": audit.get("source_origin") or "prehrajto_search",
        "db_source_exists": bool(audit.get("db_source_exists")),
        "lang_class": audit.get("db_lang_class"),
        "audio_lang": audit.get("db_audio_lang"),
    }


def build_catalog(backlog_path: Path, audits_path: Path) -> list[dict[str, Any]]:
    uploaded, dead = load_uploaded_state()
    audits = latest_audits(audits_path)
    rows_by_key: dict[str, dict[str, Any]] = {}
    for episode in load_jsonl(backlog_path):
        for candidate in episode.get("candidates") or []:
            row = catalog_row(
                episode,
                candidate,
                audit=audits.get(int(candidate["source_id"])),
                uploaded=uploaded,
                dead=dead,
            )
            rows_by_key[row["source_key"]] = row
    for audit in audits.values():
        if not audit.get("source_url") or audit.get("episode_id") is None:
            continue
        episode = {
            "series_id": audit.get("series_id"),
            "series_slug": audit.get("series_slug"),
            "series_title": audit.get("series_title"),
            "episode_id": audit.get("episode_id"),
            "season": audit.get("season"),
            "episode": audit.get("episode"),
            "episode_name": audit.get("episode_name"),
        }
        candidate = candidate_from_audit(audit)
        row = catalog_row(
            episode,
            candidate,
            audit=audit,
            uploaded=uploaded,
            dead=dead,
        )
        rows_by_key[row["source_key"]] = row
    return sorted(
        rows_by_key.values(),
        key=lambda row: (
            str(row.get("series_slug") or ""),
            int(row.get("season") or 0),
            int(row.get("episode") or 0),
            str(row.get("source_key") or ""),
        ),
    )


def import_rows(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in catalog:
        if row["status"] == "dead":
            continue
        if row["import_status"] == "already_in_db" and not row.get("language_verdict"):
            continue
        rows.append(row)
    return rows


def write_report(path: Path, catalog: list[dict[str, Any]], export_rows: list[dict[str, Any]]) -> None:
    status = Counter(row.get("status") for row in catalog)
    import_status = Counter(row.get("import_status") for row in catalog)
    quality = Counter(row.get("quality_tier") for row in catalog)
    report = {
        "catalog_count": len(catalog),
        "import_export_count": len(export_rows),
        "status": dict(status),
        "import_status": dict(import_status),
        "quality": dict(quality),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--audits", default="audits/language-audit-latest.jsonl.gz")
    ap.add_argument("--catalog-out", default="sources/discovered-episode-sources.jsonl.gz")
    ap.add_argument("--import-out", default="exports/video-source-import.jsonl.gz")
    ap.add_argument("--report", default="reports/discovered-sources.json")
    args = ap.parse_args()

    catalog = build_catalog(REPO_ROOT / args.backlog, REPO_ROOT / args.audits)
    export_rows = import_rows(catalog)
    write_jsonl(REPO_ROOT / args.catalog_out, catalog)
    write_jsonl(REPO_ROOT / args.import_out, export_rows)
    write_report(REPO_ROOT / args.report, catalog, export_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
