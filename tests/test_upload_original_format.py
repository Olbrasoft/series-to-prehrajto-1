import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import prehrajto_upload as upload


@pytest.mark.parametrize('suffix,mime', [('.mkv','video/x-matroska'), ('.mp4','video/mp4'),
                                       ('.avi','video/x-msvideo')])
def test_prepare_and_multipart_keep_original_container(monkeypatch, tmp_path, suffix, mime):
    path = tmp_path / ('source' + suffix)
    path.write_bytes(b'video')
    calls = []
    class Response:
        status_code, text = 200, '{}'
        def raise_for_status(self):pass
        def json(self):return {'params':json.dumps({'video_id':123}), 'response':'ok',
                               'project':'test','nonce':'test','signature':'test'}
    class Session:
        def get(self, *a, **kw):return Response()
        def post(self, url, **kwargs):
            calls.append((url,kwargs));return Response()
    cdn = []
    def post(url, **kwargs):
        file_entry = kwargs['files'][0][1]
        cdn.append((file_entry[0],file_entry[2]))
        return Response()
    monkeypatch.setattr(upload.requests, 'post', post)
    assert upload.upload_video(Session(),path,display_name='Example S01E02 - 2. epizoda CZ Dabing') == 123
    prep = calls[0][1]['data']
    assert prep['type'] == mime
    assert prep['name'].endswith(suffix)
    assert cdn == [(prep['name'],mime)]
    assert calls[-1][1]['data']['uploadedVideoListing-name'] == 'Example S01E02 - 2. epizoda CZ Dabing'
