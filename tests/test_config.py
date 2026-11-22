import os

import pytest

from ai_ingest.config import load_environment


def test_env_file_overrides_existing_environment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "GROQ_API_KEY",
        "malformed-existing-value\x01",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(
        "GROQ_API_KEY=synthetic-test-key\n",
        encoding="utf-8",
    )

    load_environment(env_file)

    assert os.environ["GROQ_API_KEY"] == "synthetic-test-key"


def test_missing_env_file_has_clear_error(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_environment(tmp_path / "missing.env")