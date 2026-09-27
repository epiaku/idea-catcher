from typer.testing import CliRunner

from catcher.cli import app

runner = CliRunner()


def test_help_describes_the_tool():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Idea Catcher" in result.output


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"
