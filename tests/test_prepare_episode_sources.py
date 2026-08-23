import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import prepare_episode_sources as source_preparation  # noqa: E402


def test_select_todo_shard_partitions_ranked_candidates_without_overlap():
    episodes = [{"episode_id": episode_id} for episode_id in range(1, 13)]

    shards = [
        source_preparation.select_todo_shard(
            episodes,
            shard_index=index,
            shard_count=3,
            limit=3,
        )
        for index in range(3)
    ]

    assert [[row["episode_id"] for row in shard] for shard in shards] == [
        [1, 4, 7],
        [2, 5, 8],
        [3, 6, 9],
    ]
    selected_ids = [row["episode_id"] for shard in shards for row in shard]
    assert len(selected_ids) == len(set(selected_ids))


def test_claim_episode_batch_persists_unique_shard_assignments(tmp_path):
    now = source_preparation.dt.datetime(
        2026,
        6,
        30,
        12,
        tzinfo=source_preparation.dt.timezone.utc,
    )
    path = tmp_path / "claims.jsonl"
    episodes = [
        {
            "episode_id": episode_id,
            "series_id": 10,
            "series_slug": "example",
            "series_title": "Example",
            "season": 1,
            "episode": episode_id,
        }
        for episode_id in range(1, 10)
    ]

    claims = source_preparation.claim_episode_batch(
        episodes,
        path=path,
        batch_id="batch-1",
        shard_count=3,
        limit_per_shard=2,
        ttl_minutes=180,
        now=now,
    )

    assert [row["episode_id"] for row in claims] == [1, 2, 3, 4, 5, 6]
    assert [row["claim_shard_index"] for row in claims] == [0, 1, 2, 0, 1, 2]
    assert len(source_preparation.active_preparation_claims(path, now=now)) == 6

    second = source_preparation.claim_episode_batch(
        episodes,
        path=path,
        batch_id="batch-2",
        shard_count=3,
        limit_per_shard=2,
        ttl_minutes=180,
        now=now,
    )
    assert [row["episode_id"] for row in second] == [7, 8, 9]


def test_usable_prepared_episodes_exclude_no_source_results(tmp_path):
    path = tmp_path / "prepared.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"episode_id": 1, "selected_source": None}),
                json.dumps(
                        {
                            "episode_id": 2,
                            "upload_ready": True,
                            "selected_source": {
                                "source_id": 20,
                                "signals": {
                                    "provider_probe": {
                                        "status": "ok",
                                        "streams": [{"res": 1080}],
                                    }
                                },
                            },
                    }
                ),
                json.dumps(
                        {
                            "episode_id": 3,
                            "upload_ready": True,
                            "selected_source": {
                                "source_id": 30,
                                "signals": {
                                    "provider_probe": {
                                        "status": "ok",
                                        "streams": [{"res": 1080}],
                                    }
                                },
                            },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert source_preparation.latest_usable_prepared_episode_ids(path, {30}) == {2}


def test_retry_due_respects_failed_retry_window():
    now = source_preparation.dt.datetime(
        2026,
        6,
        24,
        16,
        tzinfo=source_preparation.dt.timezone.utc,
    )

    assert source_preparation.retry_due(None, now=now)
    assert not source_preparation.retry_due(
        {"prepared_at": "2026-06-24T15:00:00Z"},
        now=now,
    )
    assert source_preparation.retry_due(
        {"prepared_at": "2026-06-23T15:00:00Z"},
        now=now,
    )


def test_uploaded_episode_exclusions_include_ids_and_keys(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "uploaded-shard-0.json").write_text(
        json.dumps(
            {
                "uploads": [
                    {
                        "episode_id": 10,
                        "series_id": 2,
                        "season": 3,
                        "episode": 4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(source_preparation, "REPO_ROOT", tmp_path)

    uploaded_ids, uploaded_keys = source_preparation.uploaded_episode_exclusions()

    assert uploaded_ids == {10}
    assert uploaded_keys == {(2, 3, 4)}


def test_compact_plan_retains_verified_fallback_candidates():
    row = {
        "episode_id": 10,
        "tested_sources": [
            {
                "source_id": 1,
                "verdict": "CZ_AUDIO",
                "score": [1, 1080, 100, 1_000_000_000, 0, -1],
                "signals": {
                    "provider_probe": {
                        "status": "ok",
                        "streams": [{"label": "1080p", "res": 1080}],
                    }
                },
            },
            {
                "source_id": 2,
                "verdict": "CZ_AUDIO",
                "score": [1, 720, 100, 800_000_000, 0, -2],
                "signals": {
                    "provider_probe": {
                        "status": "ok",
                        "streams": [{"label": "720p", "res": 720}],
                    }
                },
            },
            {
                "source_id": 3,
                "verdict": "UNKNOWN",
                "score": [1, 1080, 0, 900_000_000, 0, -3],
                "signals": {
                    "provider_probe": {
                        "status": "ok",
                        "streams": [{"label": "1080p", "res": 1080}],
                    }
                },
            },
        ],
    }

    compacted = source_preparation.compact_plan_row(row)

    assert [source["source_id"] for source in compacted["tested_sources"]] == [1, 2]


def test_source_precheck_requires_czech_audio_hint_and_upload_quality():
    good = {
        "source_title": "Dexter S07E04 CZ Dabing",
        "filesize_bytes": 550 * 1024 * 1024,
    }
    too_small = {
        "source_title": "Dexter S07E04 CZ Dabing",
        "filesize_bytes": 120 * 1024 * 1024,
    }
    not_czech = {
        "source_title": "Dexter S07E04 1080p",
        "filesize_bytes": 800 * 1024 * 1024,
    }
    db_czech = {
        "source_title": "Dexter S07E04",
        "db_lang_class": "CZ_DUB",
        "resolution_hint": "1080p",
    }

    assert source_preparation.source_has_cz_audio_hint(good)
    assert source_preparation.source_has_upload_quality_hint(good)
    assert not source_preparation.source_has_upload_quality_hint(too_small)
    assert not source_preparation.source_has_cz_audio_hint(not_czech)
    assert source_preparation.source_has_cz_audio_hint(db_czech)
    assert source_preparation.source_has_upload_quality_hint(db_czech)


def test_episode_preparation_score_prioritizes_existing_usable_sources():
    no_sources = {"episode_id": 1, "sources": []}
    weak = {
        "episode_id": 2,
        "sources": [
            {
                "source_id": 20,
                "source_title": "Example S01E02 480p",
                "resolution_hint": "480p",
            }
        ],
    }
    usable = {
        "episode_id": 3,
        "sources": [
            {
                "source_id": 30,
                "source_title": "Example S01E03 CZ Dabing",
                "filesize_bytes": 900 * 1024 * 1024,
            }
        ],
    }

    scored = sorted(
        [no_sources, weak, usable],
        key=lambda episode: tuple(
            -value for value in source_preparation.episode_preparation_score(episode)
        ),
    )

    assert [episode["episode_id"] for episode in scored] == [3, 2, 1]
    assert source_preparation.episode_preparation_score(usable, {30}) == (0, 0, 0, 0, 0)
