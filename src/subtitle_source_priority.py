"""Rank original sources by evidence, never by our generated CZ Titulky label."""

import gzip
import json
import re
from collections import Counter, defaultdict

CZ_TITLE = re.compile(r"(?i)\b(?:cz|cs|česk[eéyý]|czech)[ ._-]*(?:tit|sub)|\b(?:tit(?:ulky)?|subtitles?)[ ._-]*(?:cz|cs)\b")
BLOCKED_TARGETS = {"target_not_found", "target_processing", "target_unresolved", "target_detail_missing"}


def load_evidence(uploads, reports, prepared_path=None, audit_path=None):
    from backfill_uploaded_subtitles import matching_upload_status, source_identity, SAFE_LANGS

    sources, series = {}, defaultdict(Counter)
    wanted = {source_identity(u.get("source_url") or "") for u in uploads.values()}

    def remember(url, checked, positive):
        identity = source_identity(url or "")
        if identity and identity in wanted and checked >= sources.get(identity, {}).get("checked_at", ""):
            sources[identity] = {"checked_at": checked, "positive": positive}

    for eid, upload in uploads.items():
        previous = matching_upload_status(reports.get(eid), upload)
        original = upload.get("source_url")
        if (previous.get("status") == "uploaded" and original and previous.get("source_url")
                and source_identity(previous["source_url"]) == source_identity(original)):
            remember(original, previous.get("checked_at") or "", True)
            series[upload.get("series_id")]["positive"] += 1
        elif previous.get("original_result") == "no_czech_track":
            remember(original, previous.get("original_checked_at") or "", False)
            series[upload.get("series_id")]["negative"] += 1
        elif previous.get("original_result") == "czech_track_found":
            remember(original, previous.get("original_checked_at") or "", True)

    for path, planned in [(prepared_path, True), (audit_path, False)]:
        if path is None or not path.exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                candidates = [row.get("selected_source") or {}, *(row.get("tested_sources") or [])] if planned else [row]
                for candidate in candidates:
                    probe = (candidate.get("signals") or {}).get("provider_probe") or {}
                    if probe.get("status") != "ok" or "tracks" not in probe:
                        continue
                    positive = any(str(t.get("lang") or "").lower() in SAFE_LANGS for t in probe["tracks"])
                    remember(candidate.get("source_url"), candidate.get("audited_at") or "", positive)
    return {"sources": sources, "series": dict(series)}


def priority(row, upload, previous, cache, evidence):
    from backfill_uploaded_subtitles import source_identity

    if cache is not None and cache.available(upload):
        return (0, 0)
    if previous.get("status") in BLOCKED_TARGETS:
        return (5, 0)
    source = evidence["sources"].get(source_identity(upload.get("source_url") or ""), {})
    if source.get("positive"):
        return (1, 0)
    if previous.get("status") == "subtitle_fetch_failed":
        return (1, 0)
    # Titles and follow-up reasons are hints, not evidence of a playable track.
    hinted = bool(CZ_TITLE.search(row.get("trusted_source_title") or "") or row.get("trusted_subtitle_hint"))
    stats = evidence["series"].get(upload.get("series_id"), {})
    rate = (stats.get("positive", 0) + 1) / (sum(stats.values()) + 2)
    if hinted:
        return (2, -rate)
    return (4 if source and not source["positive"] else 3, -rate)
