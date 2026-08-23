#!/usr/bin/env python3
"""Export a small upload backlog of series episodes from the production CR DB.

The query is intentionally scoped: pick top series first, then first N episodes,
then source candidates. This avoids expensive global sorting over millions of
video_sources rows and gets the upload workflow running quickly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import gzip
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from source_quality import resolution_score, source_quality_tier

UPLOAD_LANG_CLASSES = ("CZ_DUB", "CZ_NATIVE")
REPO_ROOT = Path(__file__).resolve().parent.parent


def connect_with_retries(
    db_url: str,
    *,
    attempts: int = 40,
    delay_seconds: float = 15.0,
    max_delay_seconds: float = 60.0,
):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(db_url)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            print(f"DB connect failed on attempt {attempt}/{attempts}: {exc}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(min(max_delay_seconds, delay_seconds * attempt))
    assert last_exc is not None
    raise last_exc


def json_default(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def sxe(season: int | None, episode: int | None) -> str:
    return f"S{int(season or 0):02d}E{int(episode or 0):02d}"


def display_name(row: dict[str, Any]) -> str:
    base = f"{row['series_title']} {sxe(row['season'], row['episode'])}"
    subtitle = (row.get("episode_name") or row.get("episode_title") or "").strip()
    if subtitle and subtitle.lower() != str(row["series_title"]).lower():
        base = f"{base} - {subtitle}"
    if row.get("preferred_lang_class") == "CZ_SUB":
        return f"{base} CZ Titulky"
    return f"{base} CZ Dabing"


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("https://prehrajto.cz/", "https://prehraj.to/")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def add_episode_exclusion(row: dict[str, Any], episode_ids: set[int], episode_keys: set[str]) -> None:
    if row.get("episode_id") is not None:
        episode_ids.add(int(row["episode_id"]))
    if row.get("series_id") is not None and row.get("season") is not None and row.get("episode") is not None:
        episode_keys.add(f"{int(row['series_id'])}:{int(row['season'])}:{int(row['episode'])}")


def load_uploaded_exclusions() -> tuple[set[int], set[str]]:
    episode_ids: set[int] = set()
    episode_keys: set[str] = set()
    for path in sorted((REPO_ROOT / "state").glob("uploaded*.json")):
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for upload in data.get("uploads", []):
            add_episode_exclusion(upload, episode_ids, episode_keys)
    return episode_ids, episode_keys


def load_burned_source_exclusions() -> set[int]:
    source_ids: set[int] = set()
    paths = [REPO_ROOT / "state" / "uploaded.json"]
    paths.extend(sorted((REPO_ROOT / "state").glob("uploaded-shard-*.json")))
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for failure in data.get("failed_attempts", []):
            if failure.get("permanent") and failure.get("source_id") is not None:
                source_ids.add(int(failure["source_id"]))
    return source_ids


def fetch_rows(
    conn,
    *,
    series_limit: int,
    episode_limit: int,
    source_limit_per_episode: int,
    uploaded_episode_ids: set[int],
    uploaded_episode_keys: set[str],
    burned_source_ids: set[int],
    skip_sources: bool,
) -> list[dict[str, Any]]:
    if skip_sources:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH ranked_series AS (
                    SELECT
                        s.*,
                        row_number() OVER (
                            ORDER BY
                                coalesce(s.imdb_votes, 0) DESC,
                                coalesce(s.imdb_rating, 0) DESC,
                                coalesce(s.csfd_rating, 0) DESC,
                                s.id
                        ) AS series_rank
                    FROM series s
                    ORDER BY
                        coalesce(s.imdb_votes, 0) DESC,
                        coalesce(s.imdb_rating, 0) DESC,
                        coalesce(s.csfd_rating, 0) DESC,
                        s.id
                    LIMIT %(series_limit)s
                )
                SELECT
                    e.*,
                    s.series_rank,
                    s.title AS series_title,
                    s.original_title AS series_original_title,
                    s.slug AS series_slug,
                    s.first_air_year,
                    s.description AS series_description,
                    s.tmdb_overview_en AS series_overview_en,
                    s.imdb_id,
                    s.tmdb_id,
                    s.imdb_rating,
                    s.imdb_votes,
                    s.csfd_rating
                FROM ranked_series s
                JOIN episodes e ON e.series_id = s.id
                WHERE NOT (e.id = ANY(%(uploaded_episode_ids)s))
                ORDER BY
                    s.series_rank,
                    e.season NULLS LAST,
                    e.episode NULLS LAST,
                    e.id
                LIMIT %(episode_limit)s
                """,
                {
                    "series_limit": series_limit,
                    "episode_limit": episode_limit,
                    "uploaded_episode_ids": list(uploaded_episode_ids),
                },
            )
            episode_rows = list(cur.fetchall())
        return [
            {
                **episode_row,
                "source_id": None,
                "external_id": None,
                "source_title": None,
                "source_duration_sec": None,
                "resolution_hint": None,
                "filesize_bytes": None,
                "view_count": None,
                "lang_class": None,
                "audio_lang": None,
                "audio_confidence": None,
                "source_url": None,
                "source_rank": None,
            }
            for episode_row in episode_rows
        ]

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                s.id,
                s.title,
                s.original_title,
                s.slug,
                s.first_air_year,
                s.description,
                s.tmdb_overview_en,
                s.imdb_id,
                s.tmdb_id,
                s.imdb_rating,
                s.imdb_votes,
                s.csfd_rating
            FROM series s
            ORDER BY
                coalesce(s.imdb_votes, 0) DESC,
                coalesce(s.imdb_rating, 0) DESC,
                coalesce(s.csfd_rating, 0) DESC,
                s.id
            LIMIT %s
            """,
            (max(series_limit * 4, series_limit),),
        )
        series_rows = list(cur.fetchall())

    rows: list[dict[str, Any]] = []
    episode_count = 0
    for series_rank, series_row in enumerate(series_rows, start=1):
        if episode_count >= episode_limit:
            break
        if series_rank > series_limit * 4:
            break
        remaining = episode_limit - episode_count
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    e.*,
                    %(series_rank)s AS series_rank,
                    %(series_title)s AS series_title,
                    %(series_original_title)s AS series_original_title,
                    %(series_slug)s AS series_slug,
                    %(first_air_year)s AS first_air_year,
                    %(series_description)s AS series_description,
                    %(series_overview_en)s AS series_overview_en,
                    %(imdb_id)s AS imdb_id,
                    %(tmdb_id)s AS tmdb_id,
                    %(imdb_rating)s AS imdb_rating,
                    %(imdb_votes)s AS imdb_votes,
                    %(csfd_rating)s AS csfd_rating
                FROM episodes e
                WHERE e.series_id = %(series_id)s
                  AND NOT (e.id = ANY(%(uploaded_episode_ids)s))
                  AND NOT (
                      e.series_id::text || ':' || coalesce(e.season, -1)::text || ':' || coalesce(e.episode, -1)::text
                      = ANY(%(uploaded_episode_keys)s)
                  )
                ORDER BY e.season NULLS LAST, e.episode NULLS LAST, e.id
                LIMIT %(remaining)s
                """,
                {
                    "series_id": series_row["id"],
                    "series_rank": series_rank,
                    "series_title": series_row["title"],
                    "series_original_title": series_row["original_title"],
                    "series_slug": series_row["slug"],
                    "first_air_year": series_row["first_air_year"],
                    "series_description": series_row["description"],
                    "series_overview_en": series_row["tmdb_overview_en"],
                    "imdb_id": series_row["imdb_id"],
                    "tmdb_id": series_row["tmdb_id"],
                    "imdb_rating": series_row["imdb_rating"],
                    "imdb_votes": series_row["imdb_votes"],
                    "csfd_rating": series_row["csfd_rating"],
                    "lang_classes": list(UPLOAD_LANG_CLASSES),
                    "remaining": remaining,
                    "uploaded_episode_ids": list(uploaded_episode_ids),
                    "uploaded_episode_keys": list(uploaded_episode_keys),
                    "burned_source_ids": list(burned_source_ids),
                },
            )
            episode_rows = list(cur.fetchall())
        for episode_row in episode_rows:
            if episode_count >= episode_limit:
                break
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        vs.id AS source_id,
                        vs.external_id,
                        vs.title AS source_title,
                        vs.duration_sec AS source_duration_sec,
                        vs.resolution_hint,
                        vs.filesize_bytes,
                        vs.view_count,
                        vs.lang_class,
                        vs.audio_lang,
                        vs.audio_confidence,
                        vs.metadata->>'url' AS source_url,
                        row_number() OVER (
                            ORDER BY
                                CASE vs.lang_class WHEN 'CZ_DUB' THEN 0 WHEN 'CZ_NATIVE' THEN 1 ELSE 2 END,
                                CASE
                                    WHEN coalesce(vs.resolution_hint, '') ~* '(2160|4k|uhd)' THEN 4
                                    WHEN coalesce(vs.resolution_hint, '') ~* '1080|full.?hd' THEN 3
                                    WHEN coalesce(vs.resolution_hint, '') ~* '720|hd' THEN 2
                                    ELSE 0
                                END DESC,
                                coalesce(vs.view_count, 0) DESC,
                                vs.id
                        ) AS source_rank
                    FROM video_sources vs
                    WHERE vs.episode_id = %(episode_id)s
                      AND vs.provider_id = 2
                      AND vs.is_alive
                      AND vs.lang_class = ANY(%(lang_classes)s)
                      AND vs.metadata ? 'url'
                      AND NOT (vs.id = ANY(%(burned_source_ids)s))
                    ORDER BY
                        CASE vs.lang_class WHEN 'CZ_DUB' THEN 0 WHEN 'CZ_NATIVE' THEN 1 ELSE 2 END,
                        CASE
                            WHEN coalesce(vs.resolution_hint, '') ~* '(2160|4k|uhd)' THEN 4
                            WHEN coalesce(vs.resolution_hint, '') ~* '1080|full.?hd' THEN 3
                            WHEN coalesce(vs.resolution_hint, '') ~* '720|hd' THEN 2
                            ELSE 0
                        END DESC,
                        coalesce(vs.view_count, 0) DESC,
                        vs.id
                    LIMIT %(source_limit_per_episode)s
                    """,
                    {
                        "episode_id": episode_row["id"],
                        "lang_classes": list(UPLOAD_LANG_CLASSES),
                        "burned_source_ids": list(burned_source_ids),
                        "source_limit_per_episode": source_limit_per_episode,
                    },
                )
                source_rows = list(cur.fetchall())
            episode_count += 1
            if source_rows:
                for source_row in source_rows:
                    rows.append({**episode_row, **source_row})
            else:
                rows.append(
                    {
                        **episode_row,
                        "source_id": None,
                        "external_id": None,
                        "source_title": None,
                        "source_duration_sec": None,
                        "resolution_hint": None,
                        "filesize_bytes": None,
                        "view_count": None,
                        "lang_class": None,
                        "audio_lang": None,
                        "audio_confidence": None,
                        "source_url": None,
                        "source_rank": None,
                    }
                )
    return rows


def group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_episode: dict[int, dict[str, Any]] = {}
    for row in rows:
        eid = row["id"]
        if eid not in by_episode:
            preferred = row["lang_class"] or "UNKNOWN"
            item = {
                "episode_id": eid,
                "series_id": row["series_id"],
                "series_slug": row["series_slug"],
                "series_title": row["series_title"],
                "series_original_title": row["series_original_title"],
                "first_air_year": row["first_air_year"],
                "season": row["season"],
                "episode": row["episode"],
                "episode_code": sxe(row["season"], row["episode"]),
                "episode_title": row["title"],
                "episode_name": row["episode_name"],
                "air_date": row["air_date"].isoformat() if row["air_date"] else None,
                "runtime": row["runtime"],
                "imdb_id": row["imdb_id"],
                "tmdb_id": row["tmdb_id"],
                "imdb_rating": row["imdb_rating"],
                "imdb_votes": row["imdb_votes"],
                "csfd_rating": row["csfd_rating"],
                "preferred_lang_class": preferred,
                "series_description": row["series_description"] or "",
                "series_overview_en": row["series_overview_en"] or "",
                "source_description": row["description"] or row["overview"] or row["series_description"] or row["series_overview_en"] or "",
                "description": row["description"] or row["overview"] or row["series_description"] or "",
                "candidates": [],
            }
            item["display_name"] = display_name(item)
            by_episode[eid] = item
        item = by_episode[eid]
        if row["source_id"] is None:
            continue
        url = normalize_url(row["source_url"])
        if not url:
            continue
        candidate = {
            "source_id": row["source_id"],
            "external_id": row["external_id"],
            "url": url,
            "title": row["source_title"],
            "duration_sec": row["source_duration_sec"],
            "resolution_hint": row["resolution_hint"],
            "resolution_score": resolution_score(row["resolution_hint"]),
            "filesize_bytes": row["filesize_bytes"],
            "view_count": row["view_count"],
            "lang_class": row["lang_class"],
            "audio_lang": row["audio_lang"],
            "audio_confidence": row["audio_confidence"],
            "source_origin": "production_db",
            "db_source_exists": True,
        }
        candidate["quality_tier"] = source_quality_tier(candidate)
        item["candidates"].append(candidate)

    for item in by_episode.values():
        item["candidates"].sort(
            key=lambda c: (
                0 if c["lang_class"] in {"CZ_DUB", "CZ_NATIVE"} else 1,
                0 if c.get("quality_tier") == "preferred" else 1,
                -int(c.get("resolution_score") or 0),
                -(c.get("filesize_bytes") or 0),
                -(c.get("view_count") or 0),
                c["source_id"],
            )
        )
    return list(by_episode.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--series-limit", type=int, default=8)
    ap.add_argument("--episode-limit", type=int, default=80)
    ap.add_argument("--source-limit-per-episode", type=int, default=8)
    ap.add_argument("--skip-sources", action="store_true")
    args = ap.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or DATABASE_URL required", file=sys.stderr)
        return 2

    uploaded_episode_ids, uploaded_episode_keys = load_uploaded_exclusions()
    burned_source_ids = load_burned_source_exclusions()
    conn = connect_with_retries(args.db_url)
    try:
        rows = fetch_rows(
            conn,
            series_limit=args.series_limit,
            episode_limit=args.episode_limit,
            source_limit_per_episode=args.source_limit_per_episode,
            uploaded_episode_ids=uploaded_episode_ids,
            uploaded_episode_keys=uploaded_episode_keys,
            burned_source_ids=burned_source_ids,
            skip_sources=args.skip_sources,
        )
    finally:
        conn.close()

    episodes = group_rows(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", encoding="utf-8") as fh:
        for episode in episodes:
            fh.write(json.dumps(episode, ensure_ascii=False, default=json_default) + "\n")

    counts: dict[str, int] = defaultdict(int)
    for episode in episodes:
        counts[episode["series_title"]] += 1
    print(f"Wrote {len(episodes)} episodes to {args.out}")
    for title, count in counts.items():
        print(f"  {title}: {count}")
    print(f"  total candidate sources: {sum(len(e['candidates']) for e in episodes)}")
    print(f"  excluded uploaded episodes: ids={len(uploaded_episode_ids)} keys={len(uploaded_episode_keys)}")
    print(f"  excluded permanently failed sources: {len(burned_source_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
