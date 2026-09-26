import datetime as dt

from src.ops_watchdog import active_workflows, is_recent_active_run
from src import ops_watchdog


NOW = dt.datetime(2026, 8, 27, 8, 0, tzinfo=dt.timezone.utc)


def test_recent_queued_run_is_active():
    row = {"status": "queued", "createdAt": "2026-08-27T07:00:00Z"}

    assert is_recent_active_run(row, now=NOW)


def test_stale_queued_run_is_ignored():
    row = {"status": "queued", "createdAt": "2026-08-26T15:37:13Z"}

    assert not is_recent_active_run(row, now=NOW)


def test_unknown_creation_time_is_treated_conservatively():
    assert is_recent_active_run({"status": "in_progress"}, now=NOW)


def test_completed_run_is_not_active():
    row = {"status": "completed", "createdAt": "2026-08-27T07:59:00Z"}

    assert not is_recent_active_run(row, now=NOW)


def test_active_workflows_excludes_stale_runs(monkeypatch):
    monkeypatch.setattr("src.ops_watchdog.dt.datetime", FixedDateTime)
    report = {
        "workflow_runs": [
            {
                "workflowName": "sync",
                "status": "queued",
                "createdAt": "2026-08-26T15:37:13Z",
            },
            {
                "workflowName": "prepare-manifest",
                "status": "in_progress",
                "createdAt": "2026-08-27T07:30:00Z",
            },
        ]
    }

    assert active_workflows(report) == {"prepare-manifest"}


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_operator_pause_blocks_sync_without_calling_github(monkeypatch):
    monkeypatch.setenv("VIDEO_UPLOADS_PAUSED", "true")
    calls = []
    monkeypatch.setattr(ops_watchdog, "workflow_active_run_count", lambda name: calls.append(name) or 0)
    monkeypatch.setattr(ops_watchdog, "run_gh", lambda *args, **kwargs: calls.append(args))
    active = set()

    assert not ops_watchdog.queue_workflow("sync", {}, active=active, dry_run=False)
    assert calls == []
    assert active == set()


def test_operator_pause_does_not_block_subtitle_dispatch(monkeypatch):
    monkeypatch.setenv("VIDEO_UPLOADS_PAUSED", "true")
    monkeypatch.setattr(ops_watchdog, "workflow_active_run_count", lambda name: 0)
    dispatched = []
    monkeypatch.setattr(ops_watchdog, "run_gh", lambda args, **kwargs: dispatched.append(args))
    active = set()

    assert not ops_watchdog.queue_workflow("sync", {}, active=active, dry_run=False)
    assert ops_watchdog.queue_workflow("backfill-subtitles", {}, active=active, dry_run=False)
    assert dispatched == [["workflow", "run", "backfill-subtitles.yml"]]


def test_explicit_resume_allows_sync_dispatch(monkeypatch):
    monkeypatch.setenv("VIDEO_UPLOADS_PAUSED", "false")
    monkeypatch.setattr(ops_watchdog, "workflow_active_run_count", lambda name: 0)
    dispatched = []
    monkeypatch.setattr(ops_watchdog, "run_gh", lambda args, **kwargs: dispatched.append(args))

    assert ops_watchdog.queue_workflow("sync", {}, active=set(), dry_run=False)
    assert dispatched == [["workflow", "run", "sync.yml"]]
