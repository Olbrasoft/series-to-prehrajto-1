import sys
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backfill_uploaded_subtitles import (  # noqa: E402
    TERMINAL_STATUSES,
    normalize_srt,
    build_tasks,
    pick_czech_track,
    vtt_to_srt,
)
from resolve_stream import parse_html  # noqa: E402


def multilingual_source_html() -> str:
    return """
    <script>
      videos.push({ src: "https://cdn.example/video.mp4", type: 'video/mp4', res: '720', label: '720p' });
      var tracks = [
        { file: "https://cdn.example/eng.vtt?token=1", label: "CS - 100 - eng", kind: "captions" },
        { file: "https://cdn.example/cze.vtt?token=1", label: "CS - 101 - cze", kind: "captions" }
      ];
      var normalizedTracks = [
        { src: "https://cdn.example/eng.vtt?token=1", srclang: "cs", label: "CS - 100 - eng" },
        { src: "https://cdn.example/cze.vtt?token=1", srclang: "cs", label: "CS - 101 - cze" }
      ];
    </script>
    """


def test_track_label_overrides_generic_czech_srclang_and_deduplicates_urls():
    resolved = parse_html(multilingual_source_html(), "https://prehraj.to/example/token")

    assert [(track.lang, track.label) for track in resolved.tracks] == [
        ("eng", "CS - 100 - eng"),
        ("cze", "CS - 101 - cze"),
    ]
    assert pick_czech_track(resolved) == "https://cdn.example/cze.vtt?token=1"


def test_backfill_never_falls_back_to_an_explicitly_foreign_track():
    html = multilingual_source_html().replace("CS - 101 - cze", "CS - 101 - ger")
    resolved = parse_html(html, "https://prehraj.to/example/token")

    assert pick_czech_track(resolved) is None


def test_generated_czech_upload_filename_is_recognized_from_label():
    html = """
    <script>
      videos.push({ src: "https://cdn.example/video.mp4", type: 'video/mp4', res: '720', label: '720p' });
      var tracks = [
        { file: "https://cdn.example/generated.vtt?token=1", label: "CS - 12838243 - cs-1790089830-1" }
      ];
    </script>
    """
    resolved = parse_html(html, "https://prehraj.to/example/token")

    assert resolved.tracks[0].lang == "cs"
    assert pick_czech_track(resolved) == "https://cdn.example/generated.vtt?token=1"


@pytest.mark.parametrize('suffix', ['cs-1790093256…', 'cs-1790093256...', 'cs-093256-30'])
def test_truncated_and_short_generated_czech_names_are_recognized(suffix):
    html = '''<script>
      videos.push({ src: "https://cdn.example/video.mp4", res: '720' });
      var tracks = [{ file: "https://cdn.example/cs.vtt", label: "CS - 12839167 - SUFFIX" }];
    </script>'''.replace('SUFFIX', suffix)
    assert pick_czech_track(parse_html(html, 'https://example.test/video')) == 'https://cdn.example/cs.vtt'


def test_vtt_is_converted_to_strict_crlf_srt():
    converted = vtt_to_srt(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500 align:start\nAhoj\n\n"
        b"00:00:03.000 --> 00:00:04.000\nSvet\n"
    )

    assert converted == (
        b"1\r\n00:00:01,000 --> 00:00:02,500\r\nAhoj\r\n\r\n"
        b"2\r\n00:00:03,000 --> 00:00:04,000\r\nSvet\r\n"
    )
    assert b"\n" not in converted.replace(b"\r\n", b"")


def test_existing_srt_is_normalized_and_renumbered():
    converted = normalize_srt(
        b"9\n00:00:01.000 --> 00:00:02.500\nAhoj\n\n"
        b"10\n00:00:03,000 --> 00:00:04,000\nSvet\n"
    )

    assert converted.startswith(b"1\r\n00:00:01,000 --> 00:00:02,500")
    assert b"\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000" in converted


def test_transient_target_lookup_failures_are_retried_later():
    assert "target_not_found" not in TERMINAL_STATUSES
    assert "target_processing" not in TERMINAL_STATUSES
    assert "target_unresolved" not in TERMINAL_STATUSES
    assert "source_track_not_found" in TERMINAL_STATUSES
    assert "uploaded" in TERMINAL_STATUSES


@pytest.mark.parametrize("max_rows,limit", [(1, 10), (10, 1)])
def test_bounded_backfill_prioritizes_unseen_uploads_and_rotates_retries(
    tmp_path, monkeypatch, max_rows, limit
):
    followups = tmp_path / "followups.jsonl"
    state = tmp_path / "state.json"
    report = tmp_path / "report.jsonl"
    followups.write_text("".join(json.dumps({"episode_id": i}) + "\n" for i in range(1, 6)))
    state.write_text(json.dumps({"uploads": [
        {"episode_id": i, "prehrajto_video_id": i, "display_name": "Example CZ Titulky",
         "uploaded_at": f"2026-09-{i + 10:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]}))
    report.write_text("".join(json.dumps(row) + "\n" for row in [
        {"episode_id": 1, "status": "target_not_found", "checked_at": "2026-09-21T10:00:00Z"},
        {"episode_id": 4, "status": "target_not_found", "checked_at": "2026-09-22T10:00:00Z"},
        {"episode_id": 5, "status": "uploaded", "checked_at": "2026-09-22T10:00:00Z"},
    ]))
    args = Namespace(followup_file=followups, state_file=[state], report_file=report,
                     episode_id=[], retry_reported=False, max_rows=max_rows,
                     limit=limit, lookup="public", search_min_interval=0)
    inspected = []

    def lookup(upload, **kwargs):
        inspected.append(upload["episode_id"])
        return {"processing": False, "detail_url": "https://example.test/video",
                "resolved": SimpleNamespace(tracks=[])}

    monkeypatch.setattr("backfill_uploaded_subtitles.find_uploaded_detail", lookup)
    # Newest unseen first, older unseen next, then least recently checked retry.
    for expected in [3, 2, 1, 4]:
        tasks = build_tasks(args, None)
        assert [task[0]["episode_id"] for task in tasks] == [expected]
        with report.open("a") as fh:
            fh.write(json.dumps({"episode_id": expected, "status": "target_not_found",
                                 "checked_at": "2026-09-23T10:00:00Z"}) + "\n")
    assert inspected == [3, 2, 1, 4]


def test_only_requested_accounts_cz_subtitle_uploads_are_selected(tmp_path):
    from backfill_uploaded_subtitles import load_uploads
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"uploads": [
        {"episode_id": 1, "upload_account": "primary", "display_name": "One CZ titulky"},
        {"episode_id": 2, "upload_account": "serialy", "display_name": "Two CZ Titulky"},
        {"episode_id": 3, "upload_account": "primary", "display_name": "Three CZ Dabing"},
        {"episode_id": 4, "upload_account": "primary", "display_name": "Four SK Titulky"},
    ]}))
    assert set(load_uploads([state], "primary")) == {1}
    assert set(load_uploads([state], "serialy")) == {2}


def test_stale_report_snapshot_does_not_overwrite_new_result(tmp_path):
    from backfill_uploaded_subtitles import load_latest_status
    report = tmp_path / "report.jsonl"
    report.write_text("".join(json.dumps(row) + "\n" for row in [
        {"episode_id": 1, "checked_at": "2026-09-22T15:00:00Z", "status": "uploaded"},
        {"episode_id": 1, "checked_at": "2026-09-22T14:00:00Z", "status": "verify_failed"},
    ]))
    assert load_latest_status(report)[1]["status"] == "uploaded"


def test_both_account_backlogs_include_uploads_missing_followups(tmp_path):
    from backfill_uploaded_subtitles import pending_uploads
    state = tmp_path / "state.json"
    followups = tmp_path / "followups.jsonl"
    report = tmp_path / "report.jsonl"
    state.write_text(json.dumps({"uploads": [
        {"episode_id": 1, "upload_account": "primary", "display_name": "One CZ Titulky",
         "source_url": "https://example.test/actual-source"},
        {"episode_id": 2, "upload_account": "serialy", "display_name": "Two CZ Titulky"},
        {"episode_id": 3, "upload_account": "serialy", "display_name": "Three CZ Titulky"},
    ]}))
    followups.write_text(json.dumps({"episode_id": 1, "source_url": "https://example.test/stale-source"}) + "\n")
    report.write_text(json.dumps({"episode_id": 3, "checked_at": "2026-09-22T14:00:00Z", "status": "uploaded"}) + "\n")

    primary = pending_uploads(followups, [state], report, upload_account="primary")
    serialy = pending_uploads(followups, [state], report, upload_account="serialy")
    both = pending_uploads(followups, [state], report)
    assert [row[0]["episode_id"] for row in primary] == [1]
    assert [row[0]["episode_id"] for row in serialy] == [2]
    assert {row[0]["episode_id"] for row in both} == {1, 2}
    assert primary[0][0]["source_url"] == "https://example.test/actual-source"


def task_args(tmp_path, **overrides):
    args = dict(followup_file=tmp_path / 'followups.jsonl',
                state_file=[tmp_path / 'state.json'], report_file=tmp_path / 'report.jsonl',
                upload_account='primary', episode_id=[], retry_reported=False,
                max_rows=20, limit=10, lookup='profile-search', search_min_interval=0,
                verification_limit=100)
    args.update(overrides)
    return Namespace(**args)


def write_uploads(args, count=2):
    uploads = [dict(episode_id=i, prehrajto_video_id=i, upload_account='primary',
                    display_name=f'Series S01E{i:02d} CZ Titulky') for i in range(1, count + 1)]
    args.state_file[0].write_text(json.dumps({'uploads': uploads}))
    return uploads


def test_profile_search_requires_matching_video_id(monkeypatch):
    from backfill_uploaded_subtitles import find_profile_detail
    class Session:
        def get(self, url, **kwargs):
            assert kwargs['params'] == {'searchPhrase': 'Series S01E01 CZ Titulky'}
            return SimpleNamespace(text='<div id="snippet-uploadedVideoListing-video-99">wrong upload</div>',
                                   raise_for_status=lambda: None)
    assert find_profile_detail(Session(), {'display_name': 'Series S01E01 CZ Titulky',
                                          'prehrajto_video_id': 100}) is None


def test_tasks_stream_before_searching_rest_of_batch(tmp_path, monkeypatch):
    import backfill_uploaded_subtitles as b
    args = task_args(tmp_path)
    write_uploads(args)
    searched = []
    def find(session, upload):
        searched.append(upload['episode_id'])
        return {'detail_url': 'https://example.test/video', 'processing': False}
    monkeypatch.setattr(b, 'find_profile_detail', find)
    monkeypatch.setattr(b, 'resolve', lambda *a, **kw: SimpleNamespace(tracks=[]))
    tasks = b.iter_tasks(args, object())
    assert next(tasks)[0]['episode_id'] == 2
    assert searched == [2]
    assert next(tasks)[0]['episode_id'] == 1


@pytest.mark.parametrize('status', ['submission_pending', 'submitted', 'submission_unknown', 'subtitle_processing'])
def test_submitted_rows_are_verified_once_and_never_reuploaded(tmp_path, monkeypatch, status):
    import backfill_uploaded_subtitles as b
    args = task_args(tmp_path, retry_reported=True)
    upload = write_uploads(args, 1)[0]
    b.append_jsonl(args.report_file, b.status_row(upload, upload, status,
                  detail_url='https://example.test/video', submitted_at='2026-09-22T10:00:00Z'))
    monkeypatch.setattr(b, 'find_profile_detail', lambda *a: pytest.fail('must not search or upload again'))
    checks = []
    def verify(url, timeout):
        checks.append((url, timeout))
        return False
    monkeypatch.setattr(b, 'verify_tracks', verify)
    b.verify_submissions(args)
    assert list(b.iter_tasks(args, object())) == []
    assert checks == [('https://example.test/video', 0)]
    assert b.load_latest_status(args.report_file)[1]['status'] == 'subtitle_processing'
    monkeypatch.setattr(b, 'verify_tracks', lambda *a: True)
    b.verify_submissions(args)
    assert b.load_latest_status(args.report_file)[1]['status'] == 'uploaded'


def test_verification_cannot_write_other_account_rows(tmp_path, monkeypatch):
    import backfill_uploaded_subtitles as b
    args = task_args(tmp_path)
    b.append_jsonl(args.report_file, {'episode_id': 99, 'status': 'submitted',
                                   'upload_account': 'serialy', 'detail_url': 'https://example.test/other'})
    monkeypatch.setattr(b, 'verify_tracks', lambda *a: pytest.fail('wrong account'))
    b.verify_submissions(args)
    assert b.load_latest_status(args.report_file)[99]['status'] == 'submitted'


def test_deferred_discovery_gets_one_slot_without_starving_fresh_uploads(tmp_path, monkeypatch):
    import backfill_uploaded_subtitles as b
    args = task_args(tmp_path)
    uploads = write_uploads(args, 3)
    for upload in uploads[:2]:
        b.append_jsonl(args.report_file, b.status_row(upload, upload, 'source_search_pending'))
    monkeypatch.setattr(b, 'find_profile_detail', lambda *a: {'detail_url': 'https://example.test/video', 'processing': False})
    monkeypatch.setattr(b, 'resolve', lambda *a, **kw: SimpleNamespace(tracks=[]))
    assert [row['episode_id'] for row, _, _ in b.iter_tasks(args, object())] == [2, 3, 1]


@pytest.mark.parametrize('response_mode', ['accepted', 'timeout'])
def test_main_records_submission_before_post_and_defers_verification(tmp_path, monkeypatch, response_mode):
    import backfill_uploaded_subtitles as b
    args = task_args(tmp_path)
    upload = write_uploads(args, 1)[0]
    monkeypatch.setenv('PREHRAJTO_EMAIL', 'test@example.test')
    monkeypatch.setenv('PREHRAJTO_PASSWORD', 'test-password')
    monkeypatch.setattr(b, 'login', lambda *a: object())
    monkeypatch.setattr(b, 'verify_submissions', lambda *a: None)
    monkeypatch.setattr(b, 'iter_tasks', lambda *a: iter([(upload, upload, {
        'page': 1, 'detail_url': 'https://example.test/target',
        'resolved': SimpleNamespace(tracks=[], duration_sec=100),
    })]))
    monkeypatch.setattr(b, 'source_with_subtitles', lambda *a: ('https://example.test/source', 'https://example.test/cs.vtt'))
    monkeypatch.setattr(b, 'fetch_subtitle', lambda *a: b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAhoj\n')
    monkeypatch.setattr(b, 'verify_tracks', lambda *a: pytest.fail('must not block after POST'))
    def post(*a):
        assert b.load_latest_status(args.report_file)[1]['status'] == 'submission_pending'
        if response_mode == 'timeout':
            raise b.requests.Timeout('response lost')
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(b, 'upload_subtitle', post)
    monkeypatch.setattr(sys, 'argv', ['backfill', '--upload-account', 'primary',
        '--state-file', str(args.state_file[0]), '--report-file', str(args.report_file),
        '--followup-file', str(args.followup_file), '--defer-verification', '--allow-partial'])
    assert b.main() == 0
    result = b.load_latest_status(args.report_file)[1]
    assert result['status'] == ('submitted' if response_mode == 'accepted' else 'submission_unknown')
    assert result['submitted_at']
