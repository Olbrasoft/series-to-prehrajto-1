import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import process_whisper_review_queue as review
from merge_preparation_results import newer_review_wins

NOW = dt.datetime(2026, 10, 29, 16, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize('change,expected', [
    ({}, True),
    ({'whisper': {'status': 'resolve_failed', 'permanent': True, 'error': 'upload not found (404)'}}, False),
    ({'whisper': {'status': 'resolve_failed', 'permanent': True,
                  'error': 'HTTP 502 wrapping permanent upstream error'}}, True),
    ({'whisper': {'status': 'resolve_failed', 'permanent': True,
                  'failure_policy_version': 2, 'error': 'HTTP 502 wrapping permanent upstream error'}}, False),
    ({'checked_at': '2026-10-29T15:00:00Z'}, False),
    ({'retry_after': '2026-10-30T00:00:00Z'}, False),
    ({'retry_attempts': 3}, False),
    ({'whisper': {'status': 'stream_undersized'}}, False),
    ({'status': 'whisper_confirmed_cz'}, True),
    ({'status': 'whisper_confirmed_cz', 'recovery_version': 1}, False),
])
def test_recovery_respects_real_deletions_cooldowns_and_attempt_budget(change, expected):
    row = dict(status='whisper_failed', checked_at='2026-10-01T00:00:00Z',
               whisper={'status': 'sample_failed'})
    row.update(change)
    assert review.review_due(row, now=NOW) is expected


def test_fresh_reviews_lead_and_recovery_is_bounded_and_diverse():
    fresh = dict(episode_id=1, series_id=1, status='needs_whisper')
    recovery = [dict(episode_id=i, series_id=2 if i < 5 else i,
                     status='whisper_confirmed_cz', source_title='Example CZ Dabing', filesize_bytes=500_000_000)
                for i in range(2, 10)]
    failure = dict(episode_id=10, series_id=10, status='whisper_failed')
    rows = review.select_review_batch([*recovery, failure, fresh], limit=20, recovery_limit=4)
    assert rows[0] == fresh
    assert failure in rows
    assert len(rows) == 5
    assert len({r['series_id'] for r in rows}) == 5


def test_old_preparation_snapshot_cannot_reset_review_retry_budget():
    current = dict(episode_id=1, source_id=2, checked_at='2026-10-29T10:00:00Z',
                   retry_attempts=3, recovery_version=1, status='whisper_failed')
    incoming = dict(episode_id=1, source_id=2, created_at='2026-10-29T11:00:00Z', status='needs_whisper')
    assert newer_review_wins(current, incoming) == current
    assert newer_review_wins(incoming, current) == current


def test_recovery_checks_longer_episodes_before_likely_undersized_clips():
    clip = dict(episode_id=1, series_id=1, status='whisper_confirmed_cz',
                source_title='Example 1080p', duration_sec=600, filesize_bytes=320_000_000)
    episode = dict(episode_id=2, series_id=2, status='whisper_confirmed_cz',
                   duration_sec=2400, filesize_bytes=500_000_000)
    assert review.select_review_batch([clip, episode], limit=1, recovery_limit=1) == [episode]


def test_recovery_prioritizes_stream_size_headroom_before_original_resolution():
    marginal_hd = dict(episode_id=1, series_id=1, status='whisper_confirmed_cz',
                       source_title='Example 1080p', duration_sec=2400, filesize_bytes=330_000_000)
    larger_sd = dict(episode_id=2, series_id=2, status='whisper_confirmed_cz',
                     source_title='Example SD', duration_sec=2400, filesize_bytes=900_000_000)
    assert review.select_review_batch([marginal_hd, larger_sd], limit=1, recovery_limit=1) == [larger_sd]


def test_recovery_prefers_larger_files_with_otherwise_equal_quality():
    rows = [dict(episode_id=i, series_id=i, status='whisper_confirmed_cz',
                 source_title='Example 1080p', duration_sec=2400, filesize_bytes=i * 1_000_000_000)
            for i in [1, 2]]
    assert review.select_review_batch(rows, limit=1, recovery_limit=1) == [rows[1]]


def test_recovery_fills_remaining_slots_without_rechecking_the_same_episode():
    rows = [dict(episode_id=i, source_id=i, series_id=1, status='whisper_confirmed_cz',
                 duration_sec=2400, filesize_bytes=1_000_000_000) for i in range(1, 5)]
    rows.insert(1, dict(rows[0], source_id=99))
    selected = review.select_review_batch(rows, limit=20, recovery_limit=4)
    assert [r['episode_id'] for r in selected] == [1, 2, 3, 4]


def test_completed_review_without_usable_plan_is_rechecked_once_by_cli(tmp_path, monkeypatch):
    row = dict(episode_id=1, source_id=2, series_id=3, season=1, episode=1,
               series_slug='example', series_title='Example', provider='prehrajto',
               source_title='Example S01E01', filesize_bytes=500_000_000,
               status='whisper_confirmed_cz', checked_at='2026-09-01T00:00:00Z')
    queue = tmp_path / 'plans/whisper-review-queue.jsonl'
    review.write_jsonl(queue, [row])
    monkeypatch.setattr(review, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(review, 'uploaded_episode_exclusions', lambda: (set(), set()))
    monkeypatch.setattr(review, 'source_exclusions', lambda: (set(), set()))
    calls = []
    def audit(item, **kwargs):
        assert kwargs['use_whisper'] and kwargs['probe_stream']
        calls.append(item['source_id'])
        return dict(item, audited_at='2026-10-29T16:00:00Z', verdict='CZ_AUDIO', detected_by='whisper',
                    verification_status='whisper_confirmed_cz_audio',
                    signals={'provider_probe': {'status': 'ok', 'streams': [{'res': 720}],
                                                'stream_filesize_bytes': 400_000_000},
                             'whisper': {'status': 'ok', 'language': 'cs', 'probability': .99}})
    monkeypatch.setattr(review, 'audit_one', audit)
    monkeypatch.setattr(sys, 'argv', ['process_whisper_review_queue.py', '--limit', '4'])
    assert review.main() == 0
    assert calls == [2]
    prepared = review.load_jsonl(tmp_path / 'plans/prepared-episodes.jsonl')
    assert [r['episode_id'] for r in prepared] == [1]
    assert review.load_jsonl(queue)[0]['recovery_version'] == 1
    # A missing later plan is not permission for an unbounded retry loop.
    (tmp_path / 'plans/prepared-episodes.jsonl').unlink()
    assert review.main() == 0
    assert calls == [2]
