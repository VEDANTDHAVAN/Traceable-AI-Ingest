from typer.testing import CliRunner
from ai_ingest.cli import app

def test_status_command():
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No sources configured" in result.stdout
