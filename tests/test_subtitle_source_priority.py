import datetime as dt
import json
import sys
from types import SimpleNamespace

import pytest

import backfill_uploaded_subtitles as b
from subtitle_source_priority import load_evidence, priority
from subtitle_source_cache import SubtitleCache
from test_subtitle_pipeline import SRT, files, upload
from test_subtitle_routes import args_for


def test_generated_labels_and_foreign_audio_are_not_subtitle_evidence():
    row = upload(source_lang_class="CZ_SUB", display_name="Example CZ Titulky")
    evidence = {"sources": {}, "series": {}}
    assert priority(row, row, {}, None, evidence)[0] == 3
    row["trusted_source_title"] = "Example S07E13 CZtit"
    assert priority(row, row, {}, None, evidence)[0] == 2


def test_fetch_failure_prioritizes_only_the_recorded_original_source():
    current = upload()
    evidence = {"sources": {}, "series": {}}
    for url, expected in [(current["source_url"], 1), ("https://prehraj.to/other/old-copy", 3)]:
        assert priority(current, current, {"status": "subtitle_fetch_failed", "source_url": url}, None, evidence)[0] == expected


def test_same_original_source_evidence_can_be_shared_across_accounts():
    current = upload()
    other = upload(episode_id=87917, prehrajto_video_id=29670964, upload_account="serialy")
    reports = {other["episode_id"]: b.status_row(other, other, "uploaded", source_url=other["source_url"])}
    evidence = load_evidence({current["episode_id"]: current, other["episode_id"]: other}, reports)
    assert priority(current, current, {}, None, evidence)[0] == 1
    assert b.matching_upload_status(reports[other["episode_id"]], current) == {}


def test_track_evidence_is_bound_to_the_exact_source_and_newest_probe(tmp_path):
    current = upload()
    path = tmp_path / "prepared.jsonl"
    def candidate(url, stamp, languages):
        return {"source_url": url, "audited_at": stamp,
                "signals": {"provider_probe": {"status": "ok", "tracks": [{"lang": lang} for lang in languages]}}}
    rows = [candidate(current["source_url"], "2026-10-04T10:00:00Z", ["cs"]),
            candidate("https://prehraj.to/older/source", "2026-10-04T11:00:00Z", ["cs"])]
    path.write_text(json.dumps({"tested_sources": rows}))
    evidence = load_evidence({current["episode_id"]: current}, {}, path)
    assert priority(current, current, {}, None, evidence)[0] == 1
    path.write_text(json.dumps({"tested_sources": rows + [candidate(current["source_url"], "2026-10-04T12:00:00Z", [])]}))
    assert priority(current, current, {}, None, load_evidence({current["episode_id"]: current}, {}, path))[0] == 4


def test_original_success_is_evidence_but_alternate_success_and_stale_target_are_not():
    current = upload()
    row = b.status_row(current, current, "uploaded", source_url=current["source_url"])
    evidence = load_evidence({current["episode_id"]: current}, {current["episode_id"]: row})
    assert priority(current, current, {}, None, evidence)[0] == 1
    for changed in [{"source_url": "https://prehraj.to/alternate/abc"}, {"prehrajto_video_id": 1}]:
        evidence = load_evidence({current["episode_id"]: current}, {current["episode_id"]: {**row, **changed}})
        assert priority(current, current, {}, None, evidence)[0] == 3


def test_prepared_content_has_priority_and_blocked_targets_do_not_block_ready_targets(tmp_path):
    current = upload()
    evidence = {"sources": {}, "series": {}}
    current["trusted_source_title"] = "Example S07E13 CZtit"
    assert priority(current, current, {"status": "target_processing"}, None, evidence)[0] == 5
    cache = SubtitleCache(tmp_path)
    cache.put(current, current["source_url"], 1381, SRT)
    assert priority(current, current, {}, cache, evidence)[0] == 0


def test_source_without_tracks_is_parked_without_any_target_request(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    args = args_for(state, followup, report)
    args.source_first = True
    def no_track(row):
        row["original_result"] = "no_czech_track"
        return None, None
    monkeypatch.setattr(b, "source_with_subtitles", no_track)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: pytest.fail("No target request"))
    monkeypatch.setattr(b, "find_profile_detail", lambda *a, **kw: pytest.fail("No account search"))
    assert b.build_tasks(args, object()) == []
    result = b.load_latest_status(report)[87916]
    assert result["status"] == "alternate_search_pending"
    assert result["original_result"] == "no_czech_track"
    assert not b.fast_eligible(upload(), upload(), result)


def test_source_transport_failure_does_not_probe_target_or_claim_missing_track(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    args = args_for(state, followup, report)
    args.source_first = True
    def failed(row):
        raise b.SourceDiscoveryDeferred("source_resolve_http_429")
    monkeypatch.setattr(b, "source_with_subtitles", failed)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: pytest.fail("No target request"))
    assert b.build_tasks(args, None) == []
    result = b.load_latest_status(report)[87916]
    assert result["status"] == "source_retry_pending"
    assert not b.alternate_route(result, upload())


def test_preflight_track_is_reused_for_attachment_without_second_source_request(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    current = upload()
    calls = []
    def original(row):
        calls.append(row["episode_id"])
        return current["source_url"], "track"
    monkeypatch.setenv("PREHRAJTO_EMAIL", "test@example.test")
    monkeypatch.setenv("PREHRAJTO_PASSWORD", "test")
    monkeypatch.setattr(b, "login", lambda *a: object())
    monkeypatch.setattr(b, "source_with_subtitles", original)
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(video_id=current["prehrajto_video_id"], duration_sec=1381, tracks=[]))
    monkeypatch.setattr(b, "fetch_subtitle", lambda *a: SRT)
    monkeypatch.setattr(b, "upload_subtitle", lambda *a: SimpleNamespace(status_code=200))
    monkeypatch.setattr(b, "verify_tracks", lambda *a: True)
    monkeypatch.setattr(sys, "argv", ["backfill", "--state-file", str(state), "--followup-file", str(followup),
        "--report-file", str(report), "--prepared-file", str(tmp_path / "absent"), "--audit-file", str(tmp_path / "absent"),
        "--episode-id", str(current["episode_id"])])
    assert b.main() == 0
    assert calls == [current["episode_id"]]
    assert b.load_latest_status(report)[current["episode_id"]]["status"] == "uploaded"


def test_fresh_submission_is_verified_before_long_stuck_processing(tmp_path, monkeypatch):
    state, followup, report = files(tmp_path)
    current = upload()
    fresh = upload(episode_id=87917, prehrajto_video_id=29670964)
    state.write_text(json.dumps({"uploads": [current, fresh]}))
    now = dt.datetime.now(dt.timezone.utc)
    report.write_text("")
    for uploaded, age, checked in [(current, dt.timedelta(days=10), dt.timedelta(hours=7)),
                                   (fresh, dt.timedelta(minutes=5), dt.timedelta(minutes=5))]:
        b.append_jsonl(report, b.status_row(uploaded, uploaded, "submitted",
            submitted_at=(now-age).isoformat(), checked_at=(now-checked).isoformat(),
            detail_url=str(uploaded["episode_id"])))
    checked = []
    monkeypatch.setattr(b, "verify_tracks", lambda url, *a: checked.append(url) or True)
    args = args_for(state, followup, report)
    args.verification_limit = 1
    b.verify_submissions(args)
    assert checked == [str(fresh["episode_id"])]
    assert b.load_latest_status(report)[current["episode_id"]]["status"] == "submitted"


@pytest.mark.parametrize("response,expected", [("missing", False), ("html", False), ("srt", True)])
def test_advertised_track_must_have_downloadable_cues(monkeypatch, response, expected):
    monkeypatch.setattr(b, "resolve", lambda *a, **kw: SimpleNamespace(tracks=[SimpleNamespace(lang="cs", url="track")]))
    def fetch(url):
        if response == "missing":
            raise b.requests.HTTPError("404")
        return SRT if response == "srt" else b"<html>not available</html>"
    monkeypatch.setattr(b, "fetch_subtitle", fetch)
    assert b.verify_tracks("target", 0) == expected
