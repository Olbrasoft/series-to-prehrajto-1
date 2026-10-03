import datetime as dt
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import backfill_uploaded_subtitles as b
import prepare_subtitle_sources as discovery
import verify_subtitle_delivery as delivery
from subtitle_source_cache import SubtitleCache, cache_key
from subtitle_priority import hold_source_search

NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)
SRT = b"1\r\n00:00:01,000 --> 00:00:02,000\r\nAhoj\r\n"
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAhoj\n"


def upload(**extra):
    return {"episode_id": 87916, "upload_account": "primary", "prehrajto_video_id": 29670963,
            "display_name": "Zpatky do prace S07E13 CZ Titulky", "series_title": "Zpátky do práce",
            "season": 7, "episode": 13, "source_url": "https://prehraj.to/workin-moms/644b260abed57", **extra}


def files(tmp_path, *, source=None, status="source_track_not_found", version=0):
    state, followup, report = [tmp_path / name for name in ("state.json", "followup.jsonl", "report.jsonl")]
    current = upload()
    state.write_text(json.dumps({"uploads": [current]}))
    followup.write_text(json.dumps({**current, "source_url": source or current["source_url"],
                                   "source_title": "Workin Moms S07E13 CZtitulky"}) + "\n")
    report.write_text(json.dumps({**current, "status": status, "title_match_version": version,
                                 "checked_at": NOW.isoformat(), "detail_url": "https://prehraj.to/target/abc"}) + "\n")
    return state, followup, report


def test_localized_title_recovers_original_source_only_with_matching_identity(tmp_path):
    state, followup, report = files(tmp_path)
    [(row, _)] = b.pending_uploads(followup, [state], report)
    assert row["title_match_recheck"]
    assert b.candidate_matches_series(row, "Workin Moms S07E13 CZtitulky")
    assert not b.candidate_matches_series(row, "Workin Moms S07E12 CZtitulky")
    assert "Workin Moms S07E13" in b.query_variants(row)
    files(tmp_path, source="https://prehraj.to/workin-moms/older-upload")
    assert b.pending_uploads(followup, [state], report) == []


@pytest.mark.parametrize("status,version", [("uploaded", 0), ("target_deleted", 0), ("source_track_not_found", 2)])
def test_alias_repair_does_not_reopen_success_deleted_or_already_rechecked(tmp_path, status, version):
    state, followup, report = files(tmp_path, status=status, version=version)
    assert b.pending_uploads(followup, [state], report) == []


def test_alias_requires_correct_episode_and_does_not_match_unrelated_title():
    row = upload(trusted_source_title="Workin Moms S07E12 CZtitulky")
    assert b.series_names(row) == ["Zpátky do práce"]
    assert not b.candidate_matches_series(row, "Other Series S07E13")


@pytest.mark.parametrize("status,hours", [("target_not_found", 24), ("target_processing", 2),
                                        ("target_unresolved", 6), ("source_search_pending", 6)])
def test_target_cooldown_expires_without_losing_retries(status, hours):
    previous = {"status": status, "checked_at": NOW.isoformat()}
    assert not b.retry_due(previous, now=NOW + dt.timedelta(hours=hours - .01))
    assert b.retry_due(previous, now=NOW + dt.timedelta(hours=hours))


def test_old_submissions_are_verified_periodically_not_reposted():
    previous = {"status": "subtitle_processing", "submitted_at": (NOW - dt.timedelta(days=10)).isoformat(),
                "checked_at": NOW.isoformat()}
    assert not b.retry_due(previous, verification=True, now=NOW + dt.timedelta(minutes=30))
    assert b.retry_due(previous, verification=True, now=NOW + dt.timedelta(hours=6))


def test_prepared_file_is_bound_to_account_source_target_and_duration(tmp_path):
    cache = SubtitleCache(tmp_path)
    item = upload()
    cache.put(item, "https://prehraj.to/subtitle-source/abc", 1381, SRT)
    restored = SubtitleCache(tmp_path)
    assert restored.get(item, 1382) == ("https://prehraj.to/subtitle-source/abc", SRT)
    assert restored.get(item, 1400) is None
    assert restored.get(item, None) is None
    for change in ({"upload_account": "serialy"}, {"prehrajto_video_id": 123}, {"source_url": "replacement"}):
        assert restored.get({**item, **change}, 1381) is None
    (tmp_path / f"{cache_key(item)}.srt").write_bytes(b"corrupted")
    assert restored.get(item, 1381) is None
    assert restored.due(item, NOW)


def test_cache_removes_completed_or_replaced_uploads(tmp_path):
    cache = SubtitleCache(tmp_path)
    cache.put(upload(), "source", 1381, SRT)
    cache.prune([upload(prehrajto_video_id=123)])
    assert not cache.entries
    assert list(tmp_path.glob("*.srt")) == []


def test_cached_source_bypasses_search_cooldown_and_is_selected_first(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path, status="source_search_pending", version=2)
    cache = SubtitleCache(tmp_path / "cache")
    cache.put(upload(), "source", 1381, SRT)
    monkeypatch.setattr(b, "retry_due", lambda *a, **kw: False)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(tracks=[]))
    args = Namespace(followup_file=followup, state_file=[state], report_file=report,
                     episode_id=[], retry_reported=False, max_rows=5, limit=5,
                     lookup="profile-search", prepared_cache=cache, prepared_only=True)
    assert len(b.build_tasks(args, None)) == 1


def test_discovery_prepares_file_then_reuses_it_without_more_requests(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    calls = []
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(tracks=[], duration_sec=1381))
    monkeypatch.setattr(b, "source_with_subtitles", lambda row: calls.append(row) or (row["source_url"], "track"))
    monkeypatch.setattr(b, "fetch_subtitle", lambda url: VTT)
    args = Namespace(cache_dir=tmp_path / "cache", report_file=report, followup_file=followup,
                     state_file=[state], max_runtime=30, limit=30, max_ready=100, search_min_interval=0)
    assert discovery.prepare(args) == 0
    assert SubtitleCache(args.cache_dir).get(upload(), 1381) == (upload()["source_url"], SRT)
    assert discovery.prepare(args) == 0
    assert len(calls) == 1


def test_discovery_stops_on_rate_limit_and_keeps_backlog_retryable(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(tracks=[], duration_sec=1381))
    def unavailable(row):
        raise b.SourceDiscoveryDeferred("rate_limited")
    monkeypatch.setattr(b, "source_with_subtitles", unavailable)
    args = Namespace(cache_dir=tmp_path / "cache", report_file=report, followup_file=followup,
                     state_file=[state], max_runtime=30, limit=30, max_ready=100, search_min_interval=0)
    assert discovery.prepare(args) == 0
    cache = SubtitleCache(args.cache_dir)
    assert cache.entries[cache_key(upload())]["status"] == "deferred"
    assert not cache.due(upload(), dt.datetime.now(dt.timezone.utc))
    assert cache.due(upload(), dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=7))
    assert b.load_latest_status(report)[87916]["status"] == "source_track_not_found"


@pytest.mark.parametrize("ready,pending,hold", [(1000,100,True), (994,10000,True), (900,100,True),
                                             (899,10000,False), (1000,99,False)])
def test_subtitle_priority_keeps_upload_queue_supplied(ready, pending, hold):
    assert hold_source_search(ready, pending) is hold


def test_server_proof_downloads_cues_and_rejects_wrong_target(tmp_path, monkeypatch):
    html = """<script>'videoId': 29670963;
      videos.push({ src: "https://cdn.example/video.mp4", res: '720' });
      var tracks = [{ file: "https://cdn.example/cs.vtt?secret=1", label: "CS - 123 - cze" }];</script>"""
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, text=html, content=VTT, raise_for_status=lambda: None)
    monkeypatch.setattr(delivery.requests, "get", get)
    proof = delivery.verify("https://prehraj.to/target/abc", 29670963, SRT)
    assert proof["cue_count"] == 1
    assert proof["matches_prepared_file"]
    assert len(calls) == 2
    assert "secret" not in json.dumps(proof)
    with pytest.raises(ValueError, match="target video ID"):
        delivery.verify("https://prehraj.to/target/abc", 999)
    with pytest.raises(ValueError, match="differ"):
        delivery.verify("https://prehraj.to/target/abc", 29670963, SRT.replace(b"Ahoj", b"Other"))
