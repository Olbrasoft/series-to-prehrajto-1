"""Review can inspect upload-sized metadata, but promotion needs measured bytes."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import process_whisper_review_queue as review

MIB = 1024 ** 2


@pytest.mark.parametrize('size,accepted', [(300 * MIB - 1, False), (300 * MIB, True),
                                         (350 * MIB - 1, True), (350 * MIB, True)])
def test_review_precheck_uses_upload_minimum(size, accepted):
    row = dict(episode_id=1, source_id=2, series_id=3, season=1, episode=1,
               provider='prehrajto', filesize_bytes=size)
    selected = review.eligible_review_sources([row], uploaded_ids=set(), uploaded_keys=set(),
                                              burned=set(), prepared_ids=set())
    assert bool(selected) == accepted


def test_cli_promotes_only_measured_eligible_streams(tmp_path, monkeypatch):
    queue = tmp_path / 'plans/whisper-review-queue.jsonl'
    rows = [dict(episode_id=i, source_id=i, series_id=1, series_slug='example', series_title='Example',
                 season=1, episode=i, provider='prehrajto', source_title=f'Example S01E{i:02d}',
                 source_url=f'https://example.test/{i}', filesize_bytes=320 * MIB,
                 status='needs_whisper') for i in range(1, 6)]
    review.write_jsonl(queue, rows)
    monkeypatch.setattr(review, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(review, 'uploaded_episode_exclusions', lambda: (set(), set()))
    monkeypatch.setattr(review, 'source_exclusions', lambda: (set(), set()))
    checked = []
    def audit(row, **kwargs):
        assert kwargs['use_whisper'] and kwargs['probe_stream']
        checked.append(row['episode_id'])
        i = row['episode_id']
        measured = {1: 320 * MIB, 2: 300 * MIB - 1, 3: None, 4: 320 * MIB, 5: 320 * MIB}[i]
        return dict(row, audited_at='2026-10-28T10:00:00Z', verdict='CZ_AUDIO', detected_by='whisper',
                    verification_status='whisper_confirmed_cz_audio',
                    # Metadata must never compensate for missing measured evidence.
                    filesize_bytes=measured if measured is not None else 600 * MIB,
                    signals={'whisper': {'status': 'ok', 'language': 'cs', 'probability': .99},
                             'provider_probe': {'status': 'failed' if i == 4 else 'ok',
                                                'streams': [] if i == 5 else [{'res': 1080}],
                                                'stream_filesize_bytes': measured}})
    monkeypatch.setattr(review, 'audit_one', audit)
    monkeypatch.setattr(sys, 'argv', ['process_whisper_review_queue.py', '--limit', '5'])
    assert review.main() == 0
    assert checked == [1, 2, 3, 4, 5]
    prepared = review.load_jsonl(tmp_path / 'plans/prepared-episodes.jsonl')
    assert [r['episode_id'] for r in prepared] == [1]
    assert prepared[0]['selected_source']['filesize_bytes'] == 320 * MIB
    assert len(review.load_jsonl(queue)) == 5
