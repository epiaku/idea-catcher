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


def test_the_version_comes_from_one_place_and_is_logged_when_a_run_starts(caplog, tmp_path, make_services):
    import tomllib
    from pathlib import Path

    from catcher import __version__
    from catcher.modules.pipeline.run import RunOptions, run_pipeline

    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert "version" in pyproject["project"]["dynamic"] and "version" not in pyproject["project"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "src/catcher/__init__.py"

    caplog.set_level("INFO", logger="catcher.run")
    (tmp_path / "ideas" / "inbox").mkdir(parents=True)
    run_pipeline(tmp_path / "ideas", tmp_path / "docs", RunOptions(dry_run=True), make_services())
    assert f"run started: version={__version__} " in caplog.text
