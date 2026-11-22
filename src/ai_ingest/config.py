from pathlib import Path

from dotenv import load_dotenv


def load_environment(env_file: Path = Path(".env")) -> None:
    """Load an explicit local environment file with file precedence."""
    env_file = env_file.resolve()

    if not env_file.is_file():
        raise ValueError(
            f"Environment file not found: {env_file}"
        )

    load_dotenv(
        dotenv_path=env_file,
        override=True,
        encoding="utf-8-sig",
    )