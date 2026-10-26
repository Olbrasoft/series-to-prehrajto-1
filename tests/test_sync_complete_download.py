import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sync_batch as sync
from source_download import CompleteDownload
from download import DownloadError
from resolve_stream import StreamVariant


@pytest.mark.parametrize('limited', [True, False])
def test_worker_selects_complete_original_and_records_actual_container(monkeypatch, tmp_path, limited):
    stream = 'https://cdn.test/stream' + ('?limitsize=1000' if limited else '')
    monkeypatch.setattr(sync, 'TMP_DIR', tmp_path)
    monkeypatch.setattr(sync, 'has_probable_czech', lambda *a, **kw: (True, 'CZ'))
    monkeypatch.setattr(sync, 'resolve_stream', lambda url: SimpleNamespace(
        name='Example.mkv',videos=[StreamVariant(stream,1080,'1080p',True)]))
    monkeypatch.setattr(sync, 'head_size', lambda url: 600_000_000)
    original_calls = []
    def original(session, url, **kwargs):
        original_calls.append(url)
        return CompleteDownload('https://cdn.test/full',736_000_000,'.mkv')
    monkeypatch.setattr(sync, 'official_download', original)
    transfers = []
    def download(url, path, **kwargs):
        transfers.append((url,path.suffix,kwargs['expected_size']))
        path.write_bytes(b'complete file placeholder')
        return kwargs['expected_size']
    monkeypatch.setattr(sync, 'download_to', download)
    monkeypatch.setattr(sync, 'whisper_language', lambda path: (None,None,'disabled'))
    monkeypatch.setattr(sync, 'upload_video', lambda *a, **kw: 123)
    monkeypatch.setattr(sync, 'save_state', lambda state: None)
    monkeypatch.setattr(sync, 'push_state', lambda reason: None)
    state = {}
    episode = {'episode_id':1,'series_id':1,'series_title':'Example','season':1,'episode':2,'display_name':'Example S01E02 CZ Dabing'}
    candidate = {'source_id':1,'url':'https://prehraj.to/example/id','lang_class':'CZ_DUB'}
    assert sync.try_candidate(episode,candidate,object(),state,allow_subtitles=True,description='')
    if limited:
        assert original_calls == [candidate['url']]
        assert transfers == [('https://cdn.test/full','.mkv',736_000_000)]
    else:
        assert original_calls == []
        assert transfers == [(stream,'.mp4',600_000_000)]
    row = state['uploads'][0]
    assert row['source_download_kind'] == ('original' if limited else 'stream')
    assert row['source_container'] == ('mkv' if limited else 'mp4')
    assert row['size_bytes'] == transfers[0][2]
    assert not list(tmp_path.iterdir())


def test_unavailable_official_file_never_uploads_a_preview(monkeypatch):
    monkeypatch.setattr(sync, 'has_probable_czech', lambda *a, **kw: (True, 'CZ'))
    monkeypatch.setattr(sync, 'resolve_stream', lambda url: SimpleNamespace(
        name='Example.mkv',videos=[StreamVariant('https://cdn.test/stream?limitsize=1000',1080,'1080p',True)]))
    monkeypatch.setattr(sync, 'head_size', lambda url: 600_000_000)
    def unavailable(*a, **kw):raise DownloadError('Official download unavailable')
    monkeypatch.setattr(sync, 'official_download', unavailable)
    monkeypatch.setattr(sync, 'download_to', lambda *a, **kw: pytest.fail('Preview must not download'))
    monkeypatch.setattr(sync, 'upload_video', lambda *a, **kw: pytest.fail('Incomplete video must not upload'))
    failures = []
    monkeypatch.setattr(sync, 'record_failure', lambda *a, **kw: failures.append(kw))
    assert not sync.try_candidate({'display_name':'Example'}, {'source_id':1,'url':'https://prehraj.to/example/id'},
                                  object(),{},allow_subtitles=True,description='')
    assert failures[0]['permanent'] is False
