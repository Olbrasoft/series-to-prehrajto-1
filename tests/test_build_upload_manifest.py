import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import build_upload_manifest as manifest


def build(tmp_path, monkeypatch, *, original, alternatives, burned=(), availability=None):
    plan = {'episode_id': 7, 'series_id': 3, 'series_title': 'Example',
            'season': 1, 'episode': 2, 'upload_ready': True,
            'selected_source': original, 'tested_sources': alternatives,
            'upload_kind': 'audio', 'needs_subtitles_after_upload': False}
    prepared = tmp_path / 'prepared.jsonl'
    prepared.write_text(json.dumps(plan) + '\n')
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('')
    monkeypatch.setattr(manifest, 'load_upload_state_exclusions', lambda: (set(), set(), set(burned)))
    return manifest.build_manifest(backlog_path=empty, prepared_path=prepared,
                                   descriptions_path=empty, audits_path=empty, limit=100,
                                   require_episode_description=False, require_whisper=False,
                                   failed_availability=set(availability or ()))


def source(sid, verdict='CZ_AUDIO', size=600_000_000):
    return {'source_id': sid, 'source_url': f'https://example.test/video/{sid}',
            'source_title': f'Example S01E02 source {sid}', 'verdict': verdict,
            'filesize_bytes': size, 'detected_by': f'audit-{sid}'}


@pytest.mark.parametrize('original,burned', [
    (source(11), {11}),
    (source(11, size=100_000_000), set()),
    (source(11, size=None), set()),
])
def test_unavailable_selection_can_use_an_already_audited_source(tmp_path, monkeypatch, original, burned):
    rows, stats = build(tmp_path, monkeypatch, original=original,
                        alternatives=[source(12)], burned=burned)
    assert len(rows) == 1
    row = rows[0]
    assert [c['source_id'] for c in row['candidates']] == [12]
    for key in ('upload_job', 'source_plan', 'language_audit'):
        evidence = row['upload_manifest'][key]
        assert evidence.get('source_id', evidence.get('selected_source_id')) == 12
    assert row['upload_manifest']['source_plan']['detected_by'] == 'audit-12'
    assert row['upload_manifest']['upload_job']['source_url'].endswith('/12')
    assert stats['prepared_source_fallback'] == 1


def test_fallback_subtitle_source_uses_its_own_language_and_followup_routing(tmp_path, monkeypatch):
    rows, _ = build(tmp_path, monkeypatch, original=source(11), burned={11},
                    alternatives=[source(12, verdict='CZ_SUBTITLES_ONLY')])
    row = rows[0]
    assert row['display_name'].endswith('CZ Titulky')
    assert row['candidates'][0]['lang_class'] == 'CZ_SUB'
    for key in ('upload_job', 'source_plan'):
        assert row['upload_manifest'][key]['upload_kind'] == 'subtitles'
        assert row['upload_manifest'][key]['needs_subtitles_after_upload'] is True
    assert row['upload_manifest']['language_audit']['verdict'] == 'CZ_SUBTITLES_ONLY'


@pytest.mark.parametrize('alternative,burned,availability', [
    (source(12), {11, 12}, set()),
    (source(12, verdict='FOREIGN_AUDIO'), {11}, set()),
    (source(12, size=100_000_000), {11}, set()),
    (source(12, size=None), {11}, set()),
    (source(12), {11}, {'https://example.test/video/12'}),
])
def test_unusable_fallback_never_reopens_an_episode(tmp_path, monkeypatch, alternative, burned, availability):
    rows, _ = build(tmp_path, monkeypatch, original=source(11),
                    alternatives=[alternative], burned=burned, availability=availability)
    assert rows == []


def test_healthy_original_keeps_priority_and_audio_routing(tmp_path, monkeypatch):
    rows, stats = build(tmp_path, monkeypatch, original=source(11),
                        alternatives=[source(12, verdict='CZ_SUBTITLES_ONLY')])
    row = rows[0]
    assert row['upload_manifest']['source_plan']['selected_source_id'] == 11
    assert row['upload_manifest']['source_plan']['verdict'] == 'CZ_AUDIO'
    assert row['display_name'].endswith('CZ Dabing')
    assert row['upload_manifest']['upload_job']['needs_subtitles_after_upload'] is False
    assert not stats['prepared_source_fallback']


@pytest.mark.parametrize('measured_mib,expected', [(299, False), (300, True), (325, True), (349, True)])
def test_measured_stream_uses_actual_upload_minimum(tmp_path, monkeypatch, measured_mib, expected):
    original = source(11, size=measured_mib * 1024 * 1024)
    original['signals'] = {'provider_probe': {'status': 'ok', 'streams': [{'res': 1080}],
                                            'stream_filesize_bytes': measured_mib * 1024 * 1024}}
    rows, _ = build(tmp_path, monkeypatch, original=original, alternatives=[])
    assert bool(rows) is expected


@pytest.mark.parametrize('probe', [{}, {'status': 'failed', 'streams': [{'res': 1080}],
                                      'stream_filesize_bytes': 325 * 1024 * 1024},
                                   {'status': 'ok', 'streams': [],
                                    'stream_filesize_bytes': 325 * 1024 * 1024}])
def test_unverified_metadata_keeps_planning_margin(tmp_path, monkeypatch, probe):
    original = source(11, size=325 * 1024 * 1024)
    original['signals'] = {'provider_probe': probe}
    rows, _ = build(tmp_path, monkeypatch, original=original, alternatives=[])
    assert rows == []


def test_measured_small_stream_cannot_pass_using_optimistic_metadata(tmp_path, monkeypatch):
    original = source(11, size=800_000_000)
    original['signals'] = {'provider_probe': {'status': 'ok', 'streams': [{'res': 1080}],
                                            'stream_filesize_bytes': 200_000_000}}
    rows, _ = build(tmp_path, monkeypatch, original=original, alternatives=[])
    assert rows == []


@pytest.mark.parametrize('status,size,accepted', [('ok', 300 * 1024 ** 2, True),
    ('ok', 300 * 1024 ** 2 - 1, False), ('unavailable', 800_000_000, False),
    ('ok', None, False), ('ok', 'unknown', False)])
def test_manifest_requires_measured_complete_original_for_small_transcode(tmp_path, monkeypatch, status, size, accepted):
    original = source(11, size=800_000_000)
    original['signals'] = {'provider_probe': {'status': 'ok', 'streams': [{'res': 720}],
        'stream_filesize_bytes': 200_000_000,
        'official_original': {'status': status, 'filesize_bytes': size, 'suffix': '.avi'}}}
    rows, _ = build(tmp_path, monkeypatch, original=original, alternatives=[])
    assert bool(rows) is accepted
