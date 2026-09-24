import json
from types import SimpleNamespace

import pytest
import requests

from src import prepare_episode_sources as prep
from src.process_whisper_review_queue import eligible_review_sources, best_promotions


def source(eid, sid=None, **extra):
    return dict(episode_id=eid, source_id=sid or eid, series_id=1, season=1,
                episode=eid, provider='prehrajto', filesize_bytes=500 * 1024**2, **extra)


def test_review_skips_uploaded_aliases_burned_and_prepared_before_batch_limit():
    rows = [source(i) for i in range(1, 7)]
    rows[4]['filesize_bytes'] = 10
    eligible = eligible_review_sources(rows, uploaded_ids={1}, uploaded_keys={(1, 1, 2)},
                                       burned={3}, prepared_ids={4})
    assert [r['episode_id'] for r in eligible] == [6]
    assert len(rows) == 6  # Filtering never deletes the durable review queue.


def test_czech_audio_wins_even_when_foreign_audit_finished_later():
    def promotion(kind, sid):
        return dict(episode_id=1, upload_kind=kind,
                    selected_source=dict(source_id=sid, resolution_score=720 if kind=='audio' else 1080))
    cz = promotion('audio', 1)
    foreign = promotion('subtitles', 2)
    assert best_promotions([cz, foreign]) == [cz]
    assert best_promotions([foreign, cz]) == [cz]


def test_preparation_checkpoints_before_interruption():
    saved = []
    def prepare(episode):
        if episode == 2:
            raise KeyboardInterrupt()
        return dict(episode_id=episode, upload_ready=True)
    with pytest.raises(KeyboardInterrupt):
        prep.prepare_bounded_batch([1, 2, 3], prepare, lambda rows: saved.extend(rows))
    assert [row['episode_id'] for row in saved] == [1]


def test_preparation_time_budget_stops_before_next_episode(monkeypatch):
    ticks = iter([0, 0, 601])
    monkeypatch.setattr(prep.time, 'monotonic', lambda: next(ticks))
    saved = []
    result = prep.prepare_bounded_batch([1, 2], lambda e: dict(episode_id=e, upload_ready=True),
                                       lambda rows: saved.extend(rows), max_runtime=600)
    assert result == saved == [dict(episode_id=1, upload_ready=True)]


def test_throttled_episode_is_not_persisted_as_no_source():
    saved = []
    def prepare(episode):
        if episode == 2:
            raise prep.PreparationRateLimited('429')
        return dict(episode_id=episode, upload_ready=True)
    prep.prepare_bounded_batch([1, 2, 3], prepare, lambda rows: saved.extend(rows))
    assert [row['episode_id'] for row in saved] == [1]


def test_search_stops_query_variants_on_rate_limit(monkeypatch):
    response = requests.Response()
    response.status_code = 429
    calls = []
    def search(query, **kwargs):
        calls.append(query)
        raise requests.HTTPError(response=response)
    monkeypatch.setattr(prep, 'search_prehrajto_pages', search)
    episode = dict(series_title='Example', season=1, episode=1)
    with pytest.raises(prep.PreparationRateLimited):
        prep.live_search_candidates(episode, limit=4, query_limit=4)
    assert len(calls) == 1


def test_undersized_prepared_source_is_eligible_for_repair(tmp_path):
    path = tmp_path / 'prepared.jsonl'
    path.write_text(json.dumps(dict(episode_id=1, upload_ready=True, selected_source=dict(
        source_id=1, filesize_bytes=10, signals={'provider_probe': {'streams': [{'res':1080}]}}
    )))+'\n')
    assert prep.latest_usable_prepared_episode_ids(path, set()) == set()
