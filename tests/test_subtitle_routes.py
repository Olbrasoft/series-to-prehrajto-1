import datetime as dt
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from test_subtitle_pipeline import SRT, files, upload
import backfill_uploaded_subtitles as b
import prepare_subtitle_sources as discovery
from subtitle_source_cache import SubtitleCache
from subtitle_queue_status import queue_status


def routed(uploaded=None):
    uploaded = uploaded or upload()
    return b.status_row(uploaded, uploaded, "alternate_search_pending",
                        detail_url="https://prehraj.to/target/abc",
                        **b.handoff_fields(uploaded, target_duration=1381))


def args_for(state, followup, report):
    return Namespace(state_file=[state], followup_file=followup, report_file=report,
                     upload_account="primary", episode_id=[], retry_reported=False,
                     prepared_only=True, prepared_cache=None, max_rows=20, limit=10,
                     lookup="profile-search", search_min_interval=0)


def test_handoff_stays_out_of_fast_queue_after_six_hours_without_any_http(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    previous = routed()
    previous["checked_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).isoformat()
    report.write_text(json.dumps(previous))
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: pytest.fail("No target request for slow-owned work"))
    monkeypatch.setattr(b, "find_profile_detail", lambda *a: pytest.fail("No account lookup for slow-owned work"))
    assert b.build_tasks(args_for(state, followup, report), None) == []
    assert queue_status(followup, [state], report) == {
        "fast_due": 0, "alternate_pending": 1, "verification_due": 0, "prepared_ready": 0, "actionable": 0}


@pytest.mark.parametrize("change", ["source", "matcher", "weekly", "target"])
def test_specific_changes_reopen_original_check(change):
    current = upload()
    previous = routed(current)
    if change == "source":
        current["source_url"] = "https://prehraj.to/replacement/new-id"
    elif change == "matcher":
        previous["title_match_version"] = 1
    elif change == "weekly":
        previous["original_recheck_at"] = "2026-01-01T00:00:00Z"
    else:
        current["prehrajto_video_id"] += 1
        previous = b.matching_upload_status(previous, current)
    assert b.fast_eligible(current, current, previous)


def test_prepared_file_returns_slow_item_to_attachment_queue(tmp_path):
    cache = SubtitleCache(tmp_path)
    current = upload()
    previous = routed(current)
    assert not b.fast_eligible(current, current, previous, cache)
    cache.put(current, "https://prehraj.to/alternate/abc", 1381, SRT)
    assert b.fast_eligible(current, current, previous, cache)
    for status in b.SUBMITTED_STATUSES:
        assert not b.fast_eligible(current, current, {**previous, "status": status}, cache, force=True)


def test_route_survives_target_processing_and_rejects_wrong_account():
    current = upload()
    previous = routed(current)
    row = {**current, **{k: previous[k] for k in b.ROUTE_FIELDS}}
    update = b.status_row(row, current, "target_processing")
    assert b.alternate_route(update, current)
    assert not b.fast_eligible(row, current, update)
    assert b.matching_upload_status(previous, upload(upload_account="serialy")) == {}


def test_migration_uses_only_successfully_completed_current_matcher_checks(tmp_path):
    state, followup, report = files(tmp_path, status="source_search_pending", version=2)
    args = args_for(state, followup, report)
    original = b.load_latest_status(report)[87916]
    assert b.migrate_original_checks(args) == 1
    migrated = b.load_latest_status(report)[87916]
    assert b.alternate_route(migrated, upload())
    assert migrated["original_checked_at"] == original["checked_at"]
    assert b.migrate_original_checks(args) == 0
    for extra in ({"reason": "search_http_429"}, {"title_match_version": 0}, {"prehrajto_video_id": 1}):
        report.write_text(json.dumps({**original, **extra}))
        assert b.migrate_original_checks(args) == 0


def test_original_transport_failure_is_a_retry_not_a_handoff(tmp_path, monkeypatch):
    import sys
    state, followup, report = files(tmp_path, status="source_retry_pending", version=2)
    current = upload()
    monkeypatch.setenv("PREHRAJTO_EMAIL", "test@example.test")
    monkeypatch.setenv("PREHRAJTO_PASSWORD", "test")
    monkeypatch.setattr(b, "login", lambda *a: object())
    monkeypatch.setattr(b, "verify_submissions", lambda *a: None)
    monkeypatch.setattr(b, "iter_tasks", lambda *a: iter([(current, current, {
        "detail_url": "target", "resolved": SimpleNamespace(tracks=[], duration_sec=1381)})]))
    def failure(row):
        raise b.SourceDiscoveryDeferred("source_resolve_http_429")
    monkeypatch.setattr(b, "source_with_subtitles", failure)
    monkeypatch.setattr(b, "find_alternate_track", lambda *a, **kw: pytest.fail("Never search in attachment worker"))
    monkeypatch.setattr(sys, "argv", ["backfill", "--state-file", str(state), "--followup-file", str(followup), "--report-file", str(report)])
    assert b.main() == 0
    result = b.load_latest_status(report)[87916]
    assert result["status"] == "source_retry_pending"
    assert result["source_lane"] == "original"
    assert not b.alternate_route(result, current)
    assert not b.retry_due(result)


def test_search_resumes_after_budget_without_repeating_query_or_candidate(monkeypatch):
    now = [0]
    monkeypatch.setattr(b.time, "monotonic", lambda: now[0])
    candidates = [SimpleNamespace(title="Zpátky do práce S07E13", url=f"https://prehraj.to/source/{i}", duration_sec=1381) for i in [1, 2]]
    queries, resolved, saved = [], [], []
    monkeypatch.setattr(b, "search_pages", lambda query, **kw: queries.append(query) or [candidates])
    def resolve(url, **kw):
        resolved.append(url)
        now[0] += 10
        return SimpleNamespace(duration_sec=1381, tracks=[] if url.endswith("/1") else [SimpleNamespace(lang="cs", url="signed-track")])
    monkeypatch.setattr(b, "resolve", resolve)
    progress = {}
    def checkpoint(value):
        saved.append(json.loads(json.dumps(value)))
    with pytest.raises(b.SourceDiscoveryDeferred, match="runtime_budget"):
        b.find_alternate_track(upload(), 1381, min_interval=0, deadline=5, progress=progress, checkpoint=checkpoint)
    assert saved[-1]["candidate_index"] == 1
    restored = saved[-1]
    assert b.find_alternate_track(upload(), 1381, min_interval=0, deadline=100, progress=restored, checkpoint=checkpoint) == (candidates[1].url, "signed-track")
    assert len(queries) == 1
    assert resolved == [candidates[0].url, candidates[1].url]
    assert "signed-track" not in json.dumps(saved)


def test_transient_candidate_failure_does_not_advance_cursor(monkeypatch):
    candidate = SimpleNamespace(title="Zpátky do práce S07E13", url="https://prehraj.to/source/1", duration_sec=1381)
    monkeypatch.setattr(b, "search_pages", lambda *a, **kw: [[candidate]])
    def fail(*a, **kw):
        raise b.ResolveError("temporary", permanent=False)
    monkeypatch.setattr(b, "resolve", fail)
    progress = {}
    with pytest.raises(b.SourceDiscoveryDeferred):
        b.find_alternate_track(upload(), 1381, min_interval=0, progress=progress)
    assert progress["candidate_index"] == 0
    assert progress["seen"] == []
    monkeypatch.setattr(b, "search_pages", lambda *a, **kw: pytest.fail("Query was already saved"))
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(duration_sec=1381, tracks=[SimpleNamespace(lang="cs", url="track")]))
    assert b.find_alternate_track(upload(), 1381, min_interval=0, progress=progress) == (candidate.url, "track")


def test_slow_account_rotation_survives_restarts(tmp_path):
    cache = SubtitleCache(tmp_path)
    primary = [(upload(episode_id=i), upload(episode_id=i)) for i in range(1, 4)]
    serialy = [(upload(episode_id=4, upload_account="serialy"), upload(episode_id=4, upload_account="serialy"))]
    assert [p[1]["upload_account"] for p in discovery.fair_order(primary + serialy, cache)] == ["primary", "serialy", "primary", "primary"]
    cache.scheduler["last_account"] = "primary"
    cache.save()
    assert next(discovery.fair_order(primary + serialy, SubtitleCache(tmp_path)))[1]["upload_account"] == "serialy"


def test_slow_worker_leaves_original_queue_alone(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path, status="source_retry_pending", version=2)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: pytest.fail("Original-only work cannot enter discovery"))
    args = Namespace(cache_dir=tmp_path / "cache", report_file=report, followup_file=followup,
                     state_file=[state], max_runtime=30, limit=30, max_ready=100, search_min_interval=0)
    assert discovery.prepare(args) == 0
    assert not SubtitleCache(args.cache_dir).entries


def test_changed_duration_invalidates_prepared_file_for_rediscovery(tmp_path):
    cache = SubtitleCache(tmp_path)
    cache.put(upload(), "source", 1381, SRT)
    cache.invalidate_duration(upload(), 1400)
    assert not cache.available(upload())
    assert cache.due(upload(), dt.datetime.now(dt.timezone.utc))
