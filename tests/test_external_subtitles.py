"""Exercise allowance conservation and attachment approval boundaries."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opensubtitles_client import OpenSubtitles, ProviderStopped, identity_match
from external_subtitle_pipeline import Pipeline, approval, candidate_key, evidence_digest, submit, transport_srt, whisper
from backfill_uploaded_subtitles import load_latest_status


def client(directory):
    return OpenSubtitles(directory, "test-key", "Tests v1", session=Mock(headers={}))


def test_shared_allowance_respects_external_downloads_and_reserve(tmp_path):
    api = client(tmp_path)
    api.data["quota"] = {"requests": 30, "remaining": 70, "reset_time_utc": "2026-10-13T00:00:00Z"}
    assert api.allowance(dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)) == 60
    api.data["quota"].update(requests=90, remaining=10)
    assert api.allowance(dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)) == 0
    assert api.allowance(dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)) == 90


def test_uncertain_request_survives_reload(tmp_path):
    api = client(tmp_path)
    api.session.request.side_effect = __import__('requests').Timeout('sensitive URL')
    with pytest.raises(ProviderStopped, match='transport_Timeout'):
        api.download(42)
    assert client(tmp_path).data["quota"]["uncertain_requests"] == 1


def test_cache_reuse_does_not_call_provider(tmp_path):
    api = client(tmp_path)
    content = b'cached'
    (tmp_path/'42.srt').write_bytes(content)
    api.data['files']['42'] = {'sha256': hashlib.sha256(content).hexdigest()}
    assert api.download(42)[0] == tmp_path/'42.srt'
    api.session.request.assert_not_called()


def test_provider_key_never_goes_to_file_server(tmp_path):
    api = client(tmp_path)
    api.session.request.return_value = Mock(status_code=200)
    api.session.request.return_value.json.return_value = {'requests': 1, 'remaining': 99, 'link': 'https://files.example/signed'}
    with patch('opensubtitles_client.requests.get', return_value=Mock(status_code=200, content=b'SRT')) as get:
        api.download(42)
    assert get.call_args.kwargs['headers'] == {'User-Agent': 'Tests v1'}
    assert 'signed' not in (tmp_path/'provider.json').read_text()


def test_cached_budget_stops_before_post(tmp_path):
    api = client(tmp_path)
    api.data['quota'] = {'requests': 100, 'remaining': 0}
    with pytest.raises(ProviderStopped, match='budget_exhausted'):
        api.download(42)
    api.session.request.assert_not_called()


def test_known_imdb_cannot_be_overridden_by_alias():
    row = {'season': 2, 'episode': 2, 'imdb_id': 'tt123', 'series_title': 'The Lake'}
    attrs = {'language': 'cs', 'feature_details': {'season_number':2, 'episode_number':2, 'parent_imdb_id':456, 'parent_title':'The Lake'}}
    assert not identity_match(attrs, row)
    attrs['feature_details']['parent_imdb_id'] = 123
    assert identity_match(attrs, row)
    attrs['machine_translated'] = True
    assert not identity_match(attrs, row)


def fixture():
    row = {'episode_id':1, 'upload_account':'primary', 'target_video_id':2, 'file_id':3, 'subtitle_sha256':'hash'}
    evidence = {'target_video_id':2,'subtitle_file_id':3,'subtitle_sha256':'hash','target_duration_sec':600,
                'clips':[{'fraction':f,'status':'transcribed','segments':[{'text':'Speech'}]} for f in (.2,.5,.8)]}
    row['evidence'] = evidence
    review = {'episode_id':1,'upload_account':'primary', 'target_video_id':2,'subtitle_file_id':3,'subtitle_sha256':'hash',
              'target_duration_sec':600,'evidence_sha256':evidence_digest(evidence), 'status':'sampled_content_and_timing_verified',
              'semantic_content_confirmed':True,'timing_confirmed':True,'timing_adjustments_applied':False,
              'reviewer':'tester','checked_at':'2026-10-12',
              'reviewed_anchors':[{'fraction':f,'speech_start_sec':s+.1,'subtitle_start_sec':s,'speech_minus_subtitle_start_sec':.1} for f,s in [(.2,120),(.5,300),(.8,480)]]}
    return row, review


def test_whisper_completion_is_not_approval():
    row, _ = fixture()
    assert not approval(row, None)


@pytest.mark.parametrize('field,value',[('upload_account','serialy'),('episode_id',99),('evidence_sha256','different')])
def test_approval_cannot_cross_target_or_evidence(field,value):
    row, review = fixture()
    assert not approval(row,{**review,field:value})
    assert approval(row,review)


def test_manual_review_never_approves_retimed_track():
    row, review = fixture()
    with pytest.raises(ValueError,match='without adjustments'):
        approval(row,{**review,'timing_adjustments_applied':True})


def test_modified_file_cannot_be_submitted(tmp_path):
    pipeline = Pipeline(tmp_path)
    (tmp_path/'provider').mkdir()
    (tmp_path/'provider/3.srt').write_bytes(b'changed')
    row, _ = fixture()
    with pytest.raises(ValueError,match='missing or changed'):
        pipeline.file(row)


def test_no_post_before_durable_checkpoint(tmp_path):
    pipeline = Pipeline(tmp_path)
    pipeline.save()
    (tmp_path/'upload-plan.json').write_text(json.dumps({'candidates':['primary-2-3']}))
    with patch.dict('os.environ',{'GITHUB_ACTIONS':'false'}), patch('external_subtitle_pipeline.upload_subtitle') as post:
        with pytest.raises(ValueError,match='durable checkpoint'):
            submit(pipeline,SimpleNamespace(checkpoint=None))
        post.assert_not_called()


def test_encoding_conversion_preserves_cue_times_and_czech(tmp_path):
    path=tmp_path/'track.srt'
    path.write_bytes('1\n00:01:02,123 --> 00:01:03,456\nPříliš žluťoučký\n'.encode('cp1250'))
    converted=transport_srt(path)
    assert b'00:01:02,123 --> 00:01:03,456' in converted
    assert 'Příliš žluťoučký' in converted.decode()


def test_restore_candidate_identity_includes_account():
    row, _ = fixture()
    assert candidate_key(row) != candidate_key({**row,'upload_account':'serialy'})


def test_external_ledger_closes_deferred_work_without_rewriting_original(tmp_path):
    old={'episode_id':1,'prehrajto_video_id':2,'upload_account':'primary','status':'alternate_search_pending','checked_at':'2026-10-11'}
    new={**old,'status':'uploaded','checked_at':'2026-10-12'}
    path=tmp_path/'subtitle-backfill-status.jsonl'
    path.write_text(json.dumps(old)+'\n')
    (tmp_path/'opensubtitles-verified.jsonl').write_text(json.dumps(new)+'\n')
    assert load_latest_status(path)[1]['status']=='uploaded'
    assert json.loads(path.read_text())['status']=='alternate_search_pending'


def test_missing_ffmpeg_stops_before_model_or_network_calls(tmp_path):
    with patch('external_subtitle_pipeline.shutil.which', return_value=None):
        with pytest.raises(RuntimeError, match='FFmpeg is required'):
            whisper(Pipeline(tmp_path), SimpleNamespace())
