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


@pytest.mark.parametrize('original_size,accepted', [(300 * MIB, True), (300 * MIB - 1, False), (None, False)])
def test_original_requires_measured_minimum_before_whisper(monkeypatch, original_size, accepted):
    from types import SimpleNamespace
    import audit_language_sources as audit
    from source_download import CompleteDownload, DownloadError
    from source_quality import source_meets_size_requirement
    from prepare_episode_sources import is_resolvable

    monkeypatch.setenv('WHISPER_LANGUAGE_CHECK', '1')
    stream = SimpleNamespace(url='https://cdn.test/transcode', res=720, label='720p', is_default=True)
    resolved = SimpleNamespace(videos=[stream], tracks=[], video_id=1, name='Example.avi', duration_sec=2400)
    monkeypatch.setattr(audit, 'resolve_prehrajto', lambda *a, **kw: resolved)
    monkeypatch.setattr(audit, 'head_size', lambda _: 200 * MIB)
    def original(*a, **kw):
        if original_size is None:
            raise DownloadError('URL with secret must never be persisted')
        return CompleteDownload('https://cdn.test/original?secret=temporary', original_size, '.avi')
    monkeypatch.setattr(audit, 'official_download', original)
    samples = []
    def sample(url, *a, **kw):
        samples.append(url)
        return True, 'ok'
    monkeypatch.setattr(audit, 'sample_audio', sample)
    monkeypatch.setattr(audit, 'whisper_language', lambda *a, **kw: ('cs', .99, 'ok'))
    probe = audit.audit_provider(dict(provider='prehrajto', source_url='https://prehraj.to/example'),
        sample_seconds=30, probe_stream=True, minimum_stream_bytes=300 * MIB, original_session=object())
    result = dict(filesize_bytes=500 * MIB, signals={'provider_probe': probe})
    assert review.audit_has_upload_size(result) is accepted
    assert source_meets_size_requirement(result) is accepted
    assert is_resolvable(result) is accepted
    assert samples == (['https://cdn.test/original?secret=temporary'] if accepted else [])
    assert 'secret' not in json.dumps(probe)


def test_cli_recovers_rejected_stream_using_original_and_persists_evidence(tmp_path, monkeypatch):
    row = dict(episode_id=1, source_id=2, series_id=3, season=1, episode=1, series_slug='example',
               series_title='Example', provider='prehrajto', source_title='Example S01E01',
               filesize_bytes=400 * MIB, status='whisper_failed', whisper={'status': 'stream_undersized'})
    queue = tmp_path / 'plans/whisper-review-queue.jsonl'
    review.write_jsonl(queue, [row])
    monkeypatch.setattr(review, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(review, 'uploaded_episode_exclusions', lambda: (set(), set()))
    monkeypatch.setattr(review, 'source_exclusions', lambda: (set(), set()))
    monkeypatch.setenv('UPLOAD_PREHRAJTO_EMAIL', 'example@example.test')
    monkeypatch.setenv('UPLOAD_PREHRAJTO_PASSWORD', 'test-only')
    session = object()
    monkeypatch.setattr(review, 'login', lambda *a: session)
    calls = []
    original = dict(status='ok', filesize_bytes=400 * MIB, suffix='.avi')
    def audit(item, **kwargs):
        assert kwargs['original_session'] is session
        calls.append(item['episode_id'])
        return dict(item, audited_at='2026-10-30T18:00:00Z', verdict='CZ_AUDIO',
                    detected_by='whisper', verification_status='whisper_confirmed_cz_audio',
                    signals={'provider_probe': dict(status='ok', streams=[{'res': 720}],
                              stream_filesize_bytes=200 * MIB, official_original=original),
                             'whisper': dict(status='ok', language='cs', probability=.99)})
    monkeypatch.setattr(review, 'audit_one', audit)
    monkeypatch.setattr(sys, 'argv', ['process_whisper_review_queue.py', '--limit', '4'])
    assert review.main() == 0
    assert calls == [1]
    assert review.load_jsonl(queue)[0]['official_original'] == original
    prepared = review.load_jsonl(tmp_path / 'plans/prepared-episodes.jsonl')[0]
    assert prepared['upload_ready']
    from prepare_episode_sources import is_resolvable
    assert is_resolvable(prepared['selected_source'])
    assert review.main() == 0
    assert calls == [1]


@pytest.mark.parametrize('timeout', [True, False])
def test_audio_errors_are_bounded_and_never_persist_signed_urls(tmp_path, monkeypatch, timeout):
    import subprocess
    from types import SimpleNamespace
    import audit_language_sources as audit
    def run(cmd, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 180)
        return SimpleNamespace(returncode=1, stderr='Failed https://cdn.test/original?token=private Server returned 403')
    monkeypatch.setattr(audit.subprocess, 'run', run)
    ok, message = audit.sample_audio('https://cdn.test/original?token=private', tmp_path / 'sample.wav',
                                     start_sec=60, seconds=30)
    assert not ok
    assert 'private' not in message and 'https://' not in message
