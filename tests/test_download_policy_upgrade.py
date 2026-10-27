import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import download_policy as policy
import pick_next_episode as picker
import build_upload_manifest as manifest
import sync_batch as sync


def failure(reason='download_failed: curl exit 18: early EOF', **kwargs):
    return {'source_id':42,'episode_id':3,'reason':reason,'permanent':False,
            'failed_at':dt.datetime.now(dt.timezone.utc).isoformat(),**kwargs}


@pytest.mark.parametrize('reason', ['download_failed: curl exit 18: early EOF',
                                    'download_failed: curl exit 56: TLS EOF'])
def test_only_unversioned_interrupted_downloads_reopen(reason, tmp_path, monkeypatch):
    row=failure(reason)
    assert policy.legacy_preview_failure(row)
    assert picker.cooling_down_source_ids({'failed_attempts':[row]}) == set()
    state=tmp_path/'state';state.mkdir()
    (state/'uploaded-shard-0.json').write_text(json.dumps({'failed_attempts':[row]}))
    monkeypatch.setattr(manifest,'REPO_ROOT',tmp_path)
    assert manifest.load_upload_state_exclusions()[2] == set()


@pytest.mark.parametrize('row', [failure(download_policy_version=1), failure(download_policy_version=2),
    failure('download_failed: curl exit 28: slow'), failure('upload_failed: unknown response'),
    failure('resolve_failed: unavailable'), failure('complete_source_unavailable: HTTP 403')])
def test_new_and_unrelated_failures_keep_cooldown_in_both_selectors(row, tmp_path, monkeypatch):
    assert not policy.legacy_preview_failure(row)
    assert picker.cooling_down_source_ids({'failed_attempts':[row]}) == {42}
    state=tmp_path/'state';state.mkdir()
    (state/'uploaded-shard-0.json').write_text(json.dumps({'failed_attempts':[row]}))
    monkeypatch.setattr(manifest,'REPO_ROOT',tmp_path)
    assert manifest.load_upload_state_exclusions()[2] == {42}


def test_permanent_failures_are_never_reopened(tmp_path, monkeypatch):
    row=failure(permanent=True)
    assert not policy.legacy_preview_failure(row)
    assert picker.burned_source_ids({'failed_attempts':[row]}) == {42}
    state=tmp_path/'state';state.mkdir()
    (state/'uploaded-shard-0.json').write_text(json.dumps({'failed_attempts':[row]}))
    monkeypatch.setattr(manifest,'REPO_ROOT',tmp_path)
    assert manifest.load_upload_state_exclusions()[2] == {42}


def test_new_failure_closes_the_retry_even_with_legacy_history():
    rows=[failure(),failure(download_policy_version=1)]
    assert picker.cooling_down_source_ids({'failed_attempts':rows}) == {42}


def test_uploaded_episode_stays_closed_after_legacy_download_failure():
    episode={'episode_id':3,'series_id':1,'season':1,'episode':2,'candidates':[{'source_id':42}]}
    state={'uploads':[episode],'failed_attempts':[failure()]}
    assert picker.pick_next(state,[episode]) is None


def test_worker_tags_new_failures_for_normal_cooldown(monkeypatch):
    monkeypatch.setattr(sync,'save_state',lambda state:None)
    state={}
    sync.record_failure(state,{'episode_id':3,'series_id':1,'display_name':'Example'},
                        {'source_id':42},'download_failed: curl exit 18: EOF',permanent=False)
    row=state['failed_attempts'][0]
    assert row['download_policy_version']==policy.DOWNLOAD_POLICY_VERSION
    assert not policy.legacy_preview_failure(row)
