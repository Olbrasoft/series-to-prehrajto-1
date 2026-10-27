"""Exercise recovery through the real claim CLI and persisted upload state."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import prepare_episode_sources as prep


@pytest.mark.parametrize("failure_kind,expected", [
    ("recent", [12]), ("expired", [13]), ("permanent", [12, 13]),
    ("uploaded", [13]), ("legacy_preview", [13]),
])
def test_claim_recovers_failed_cached_source_without_duplicates(tmp_path, monkeypatch, failure_kind, expected):
    now = dt.datetime.now(dt.timezone.utc)
    episode = dict(episode_id=12, series_id=1, series_slug="example", series_title="Example",
                   season=1, episode=12)
    unseen = {**episode, "episode_id": 13, "series_id": 2, "episode": 13}
    source = dict(source_id=42, filesize_bytes=600_000_000,
                  signals={"provider_probe": {"status": "ok", "streams": [{"res": 1080}],
                                             "stream_filesize_bytes": 600_000_000}})
    plan = {**episode, "prepared_at": now.isoformat(), "upload_ready": True, "selected_source": source}
    failure = dict(episode_id=12, source_id=42, reason="complete_source_unavailable",
                   download_policy_version=1, failed_at=now.isoformat(), permanent=False)
    if failure_kind == "expired":
        failure["failed_at"] = (now - dt.timedelta(hours=7)).isoformat()
    if failure_kind == "permanent":
        failure["permanent"] = True
    if failure_kind == "legacy_preview":
        failure.pop("download_policy_version")
        failure["reason"] = "download_failed: curl exit 18: partial file"
    state = {"failed_attempts": [failure], "uploads": [episode] if failure_kind == "uploaded" else []}
    (tmp_path / "state").mkdir()
    (tmp_path / "state/uploaded-shard-0.json").write_text(json.dumps(state))
    prep.write_jsonl(tmp_path / "backlog.jsonl", [episode, unseen])
    prep.write_jsonl(tmp_path / "prepared.jsonl", [plan])
    prep.write_jsonl(tmp_path / "manifest.jsonl", [{**episode, "source_plan": {"selected_source_id": 42}}])
    monkeypatch.setattr(prep, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare_episode_sources.py", "--queue", str(tmp_path / "empty.jsonl"),
        "--backlog", str(tmp_path / "backlog.jsonl"), "--out", str(tmp_path / "prepared.jsonl"),
        "--upload-manifest", str(tmp_path / "manifest.jsonl"), "--claim-file", str(tmp_path / "claims.jsonl"),
        "--claim-batch-id", "recovery", "--claim-only", "--episode-limit", str(len(expected))])
    assert prep.main() == 0
    assert [row["episode_id"] for row in prep.load_jsonl(tmp_path / "claims.jsonl")] == expected


def test_failed_cached_source_is_excluded_while_discovery_finds_replacement(monkeypatch):
    ep = dict(episode_id=12, series_id=1, series_slug="example", series_title="Example",
              season=1, episode=12, sources=[])
    def candidate(sid):
        return dict(**{key: value for key, value in ep.items() if key != "sources"},
                    source_id=sid, provider="prehrajto", source_url=f"https://example.test/{sid}",
                    source_title="Example S01E12 CZ Dabing", filesize_bytes=600_000_000)
    ep["sources"] = [candidate(42)]
    original = prep.audit_one
    audited_ids = []
    def audit(source, **kwargs):
        audited_ids.append(source["source_id"])
        row = original(source, use_whisper=False, sample_seconds=20, probe_stream=False)
        if kwargs["probe_stream"]:
            row["signals"]["provider_probe"] = {"status": "ok", "streams": [{"res": 1080}],
                                                  "stream_filesize_bytes": 600_000_000}
        return row
    def search(*args, **kwargs):
        assert 42 in kwargs["burned"]
        return [candidate(43)]
    monkeypatch.setattr(prep, "audit_one", audit)
    monkeypatch.setattr(prep, "live_search_candidates", search)
    plan = prep.prepare_episode(ep, use_whisper=False, sample_seconds=20, source_limit=4,
        burned={42}, require_resolvable_source=True, live_search=True,
        live_search_limit=4, live_search_query_limit=4)
    assert plan["upload_ready"]
    assert plan["selected_source"]["source_id"] == 43
    assert 42 not in audited_ids
