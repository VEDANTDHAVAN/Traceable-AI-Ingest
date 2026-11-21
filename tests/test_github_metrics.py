from datetime import datetime, timezone

import pytest

from ai_ingest.github_metrics import (
    GitHubError,
    rate_delay,
    validate_metadata,
)


NOW = datetime(
    2026,
    1,
    1,
    tzinfo=timezone.utc,
)


def payload(stars=12):
    return {
        "full_name": "Fixture/Repository",
        "html_url": "https://github.com/Fixture/Repository",
        "stargazers_count": stars,
        "description": "Synthetic fixture",
        "homepage": None,
        "default_branch": "main",
        "archived": False,
        "fork": False,
    }


def test_valid_metrics():
    result = validate_metadata(payload())

    assert result["github_stars"] == 12
    assert result["repository_url"] == (
        "https://github.com/fixture/repository"
    )


@pytest.mark.parametrize("stars", [-1, True, 1.5, "12"])
def test_invalid_stars_rejected(stars):
    with pytest.raises(GitHubError):
        validate_metadata(payload(stars))


def test_zero_stars_is_valid():
    result = validate_metadata(payload(0))

    assert result["github_stars"] == 0


def test_repository_identity_mismatch_rejected():
    value = payload()
    value["html_url"] = "https://github.com/other/repository"

    with pytest.raises(GitHubError):
        validate_metadata(value)


def test_retry_after_honored():
    assert rate_delay(
        {"Retry-After": "90"},
        now=NOW,
    ) == 90.0


def test_primary_reset_honored():
    delay = rate_delay(
        {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(
                int(NOW.timestamp()) + 120
            ),
        },
        now=NOW,
    )

    assert delay == 121.0


def test_secondary_limit_default():
    assert rate_delay({}, now=NOW) == 60.0