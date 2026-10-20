import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import prepare_episode_sources as prep
import audit_language_sources as audit
from prehrajto_search import SearchResult
from language_checks import title_language_hint


def episode():
    return dict(series_id=1, series_slug="zlocinne-umysly", series_title="Zákon a pořádek: Zločinné úmysly",
                series_original_title="Law & Order: Criminal Intent", episode_id=12, season=4, episode=12, sources=[])


def result(title, sid=-1):
    return SearchResult(sid, str(abs(sid)), f"https://example.test/{abs(sid)}", title, 2700, "720p", 650_000_000)


@pytest.mark.parametrize("title,expected", [
    ("Zákon pořádek - Zločinné úmysly S04E12 czdabing", True),
    ("Zločinné úmysly 04x12 CZ", True),
    ("Law and Order Criminal Intent S4E12", True),
    ("Zločinné úmyslyS04E12 CZ", True),
    ("Zákon a pořádek Útvar pro zvláštní oběti S04E12", False),
    ("Zločinné úmysly S04E120", False),
    ("Zločinné úmysly S04E01", False),
    ("Zločinné úmysly S05E12", False),
    ("Zločinné úmysly bonus S04E12", False),
])
def test_episode_match_accepts_spelling_variants_but_rejects_wrong_episode_and_spinoff(title, expected):
    assert prep.title_matches_episode(title, episode()) is expected


@pytest.mark.parametrize("title,hint", [("Example czdabing", "cz_audio_title"),
    ("Example CZ_DABING", "cz_audio_title"), ("Example cztitulky", "cz_subtitle_title"),
    ("Example CZ titulky", "cz_subtitle_title")])
def test_compact_language_labels(title, hint):
    assert title_language_hint(title) == hint


def test_search_continues_past_subtitles_and_burned_audio(monkeypatch):
    calls = []
    sub = result("Zločinné úmysly S04E12 cztitulky")
    burned = result("Zločinné úmysly S04E12 CZ Dabing", -2)
    good = result("Zákon pořádek Zločinné úmysly S04E12 czdabing", -3)

    def search(query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            assert kwargs['should_fetch_next']([sub, burned])
            return [[sub, burned]]
        assert not kwargs['should_fetch_next']([good])
        return [[good]]

    monkeypatch.setattr(prep, 'search_prehrajto_pages', search)
    found = prep.live_search_candidates(episode(), limit=4, query_limit=4, burned={-2})
    assert [s['source_id'] for s in found] == [-3, -1]
    assert len(calls) == 2


def test_batches_cover_other_series_before_exhausting_one():
    rows = [dict(episode_id=i, series_id=1) for i in range(10)] + [dict(episode_id=10, series_id=2)]
    ordered = prep.diversify_series(rows)
    assert [r['episode_id'] for r in ordered[:3]] == [0, 1, 10]
    assert len(ordered) == len(rows)


def test_failed_first_czech_stream_does_not_force_subtitles(monkeypatch):
    ep = episode()
    ep['sources'] = [prep.search_result_to_queue_item(result(f"Zločinné úmysly S04E12 {label}", sid), ep)
                     for sid, label in [(-1, "CZ Dabing"), (-2, "CZ Dabing"), (-3, "CZ Titulky")]]
    ep["sources"][0]["filesize_bytes"] = 800_000_000
    probed = []
    original = prep.audit_one

    def audit_one(source, **kwargs):
        if not kwargs['probe_stream']:
            return original(source, **kwargs)
        probed.append(source['source_id'])
        row = original(source, use_whisper=False, sample_seconds=20, probe_stream=False)
        row['signals']['provider_probe'] = ({'status': 'ok', 'streams': [{'res': 720}]}
                                          if source['source_id'] != -1 else {'status': 'failed'})
        return row

    monkeypatch.setattr(prep, 'audit_one', audit_one)
    plan = prep.prepare_episode(ep, use_whisper=False, sample_seconds=20, source_limit=4,
                                burned=set(), require_resolvable_source=True, live_search=False,
                                live_search_limit=4, live_search_query_limit=4)
    assert plan['upload_ready']
    assert plan['selected_source']['source_id'] == -2
    assert not plan['needs_subtitles_after_upload']
    assert probed == [-1, -2]


def test_whisper_audit_probes_once_and_restores_environment(monkeypatch):
    calls = []
    monkeypatch.setenv('WHISPER_LANGUAGE_CHECK', 'old')
    def provider(item, **kwargs):
        calls.append((kwargs, os.environ['WHISPER_LANGUAGE_CHECK']))
        return {'whisper': {'status': 'ok', 'language': 'cs'}}
    monkeypatch.setattr(audit, 'audit_provider', provider)
    source = prep.search_result_to_queue_item(result('Zločinné úmysly S04E12'), episode())
    audited = audit.audit_one(source, use_whisper=True, sample_seconds=20, probe_stream=True)
    assert audited['verdict'] == 'CZ_AUDIO'
    assert len(calls) == 1
    assert calls[0][1] == '1'
    assert os.environ['WHISPER_LANGUAGE_CHECK'] == 'old'


def test_real_stream_size_overrides_optimistic_search_metadata(monkeypatch):
    source = prep.search_result_to_queue_item(result('Zločinné úmysly S04E12 CZ Dabing'), episode())
    monkeypatch.setattr(audit, 'audit_provider', lambda *a, **kw: {
        'status': 'ok', 'streams': [{'res': 480}], 'stream_filesize_bytes': 243_826_066})
    audited = audit.audit_one(source, use_whisper=False, sample_seconds=20, probe_stream=True)
    assert audited['filesize_bytes'] == 243_826_066
    assert not prep.is_resolvable(audited)


def test_preparation_tries_next_czech_source_when_first_actual_stream_is_too_small(monkeypatch):
    ep = episode()
    ep['sources'] = [prep.search_result_to_queue_item(result('Zločinné úmysly S04E12 CZ Dabing', sid), ep)
                     for sid in (-1, -2)]
    ep['sources'][0]['filesize_bytes'] = 800_000_000
    original = prep.audit_one
    probed = []

    def audit_one(source, **kwargs):
        row = original(source, use_whisper=False, sample_seconds=20, probe_stream=False)
        if kwargs['probe_stream']:
            probed.append(source['source_id'])
            size = 243_826_066 if source['source_id'] == -1 else 700_000_000
            row['signals']['provider_probe'] = {'status': 'ok', 'streams': [{'res': 720}],
                                               'stream_filesize_bytes': size}
            row['filesize_bytes'] = size
        return row

    monkeypatch.setattr(prep, 'audit_one', audit_one)
    plan = prep.prepare_episode(ep, use_whisper=False, sample_seconds=20, source_limit=4,
                                burned=set(), require_resolvable_source=True, live_search=False,
                                live_search_limit=4, live_search_query_limit=4)
    assert plan['upload_ready']
    assert plan['selected_source']['source_id'] == -2
    assert probed == [-1, -2]


def historical_plan(ep, sid=-1):
    s = prep.search_result_to_queue_item(result('Zločinné úmysly S04E12 CZ Dabing 1080p', sid), ep)
    s['resolution_hint'] = '1080p'
    s['filesize_bytes'] = None
    s.update(verdict='CZ_AUDIO', db_lang_class='CZ_DUB')
    return {'episode_id': ep['episode_id'], 'upload_ready': True, 'selected_source': s}


def test_recovered_prepared_hd_source_precedes_unseen_discovery():
    ep = episode()
    plan = historical_plan(ep)
    prep.restore_prepared_candidates([ep], {ep['episode_id']: plan}, set())
    unseen = {**episode(), 'episode_id': 13, 'sources': []}
    assert sorted([unseen, ep], key=prep.prepared_revalidation_priority) == [ep, unseen]
    assert ep['sources'][0]['source_url'] == plan['selected_source']['source_url']
    assert ep['sources'][0]['provider'] == 'prehrajto'


@pytest.mark.parametrize('change,burned', [({'source_title': 'Other show S04E12 CZ Dabing 1080p'}, set()),
                                          ({'provider': 'sktorrent'}, set()),
                                          ({}, {-1}),
                                          ({'source_title': 'Zločinné úmysly S04E13 CZ Dabing 1080p'}, set()),
                                          ({'source_title': 'Zločinné úmysly S04E12 EN 1080p',
                                            'db_lang_class': None}, set())])
def test_prepared_revalidation_never_restores_wrong_or_burned_sources(change, burned):
    ep = episode()
    plan = historical_plan(ep)
    plan['selected_source'].update(change)
    prep.restore_prepared_candidates([ep], {ep['episode_id']: plan}, burned)
    assert ep['sources'] == []


def test_working_historical_czech_hd_source_skips_live_search(monkeypatch):
    ep = episode()
    prep.restore_prepared_candidates([ep], {ep['episode_id']: historical_plan(ep)}, set())
    original = prep.audit_one
    def audited(item, **kwargs):
        row = original(item, use_whisper=False, sample_seconds=20, probe_stream=False)
        if kwargs['probe_stream']:
            row['filesize_bytes'] = 600_000_000
            row['signals']['provider_probe'] = {'status': 'ok', 'streams': [{'res': 1080}],
                                               'stream_filesize_bytes': 600_000_000}
        return row
    monkeypatch.setattr(prep, 'audit_one', audited)
    monkeypatch.setattr(prep, 'live_search_candidates', lambda *a, **kw: pytest.fail('Unnecessary search'))
    plan = prep.prepare_episode(ep, use_whisper=False, sample_seconds=20, source_limit=4,
                                burned=set(), require_resolvable_source=True, live_search=True,
                                live_search_limit=4, live_search_query_limit=4)
    assert plan['upload_ready']
    assert plan['selected_source']['source_id'] == -1
    assert plan['selected_source']['resolution_score'] == 1080


def test_unavailable_historical_source_still_uses_live_discovery(monkeypatch):
    ep = episode()
    prep.restore_prepared_candidates([ep], {ep['episode_id']: historical_plan(ep)}, set())
    good = prep.search_result_to_queue_item(result('Zločinné úmysly S04E12 CZ Dabing', -2), ep)
    original = prep.audit_one
    searches = []
    def audited(item, **kwargs):
        row = original(item, use_whisper=False, sample_seconds=20, probe_stream=False)
        if kwargs['probe_stream']:
            row['signals']['provider_probe'] = ({'status': 'ok', 'streams': [{'res': 1080}],
                                                'stream_filesize_bytes': 600_000_000}
                                               if item['source_id'] == -2 else {'status': 'failed'})
        return row
    def search(*args, **kwargs):
        searches.append(True)
        return [good]
    monkeypatch.setattr(prep, 'audit_one', audited)
    monkeypatch.setattr(prep, 'live_search_candidates', search)
    plan = prep.prepare_episode(ep, use_whisper=False, sample_seconds=20, source_limit=4,
                                burned=set(), require_resolvable_source=True, live_search=True,
                                live_search_limit=4, live_search_query_limit=4)
    assert searches == [True]
    assert plan['upload_ready']
    assert plan['selected_source']['source_id'] == -2
