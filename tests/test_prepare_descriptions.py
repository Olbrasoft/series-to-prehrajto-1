import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prepare_descriptions import KeyQuotaLimiter, is_daily_quota, key_id, pacific_day


def test_generic_resource_exhausted_is_not_daily_quota() -> None:
    assert not is_daily_quota([], "Resource has been exhausted (e.g. check quota).", None)


def test_explicit_requests_per_day_quota_is_daily() -> None:
    details = [
        {
            "violations": [
                {
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                }
            ]
        }
    ]

    assert is_daily_quota(details, "Quota exceeded", 30.0)


def test_ambiguous_persisted_lock_is_cleared(tmp_path: Path) -> None:
    state_path = tmp_path / "quota.json"
    key = "test-key"
    today = pacific_day()
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": {
                    f"gemma-4-31b-it:{key_id(key)}": {
                        "pacific_day": today,
                        "requests_today": 10,
                        "disabled_until_epoch": time.time() + 3600,
                        "disabled_reason": "Resource has been exhausted (e.g. check quota).",
                        "quota_names": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    limiter = KeyQuotaLimiter(
        [key],
        model="gemma-4-31b-it",
        rpm_per_key=1,
        tpm_per_key=0,
        rpd_per_key=1400,
        daily_safety_reserve=50,
        state_path=state_path,
    )

    with patch("prepare_descriptions.pacific_day", return_value=today):
        assert limiter.key_available(0)
