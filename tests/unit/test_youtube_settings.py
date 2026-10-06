from catcher.cli import _facts_dir_of
from catcher.core.config import Settings
from catcher.modules.youtube import access as access_mod
from catcher.modules.youtube.access import build_access
from catcher.modules.youtube.pg_gate import PostgresGate


def test_the_defaults_are_the_agreed_ones(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("YOUTUBE_"):
            monkeypatch.delenv(name)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.youtube_request_delay_s == 10 and settings.youtube_min_gap_s == 120
    assert settings.youtube_gap_jitter_s == 300 and settings.youtube_block_hours == 6
    assert settings.youtube_offline is False and settings.youtube_skip_manifests is False
    assert settings.youtube_wait_max_s == 1800 and settings.youtube_negative_ttl_h == 24


def test_every_setting_can_be_changed_with_an_environment_variable(monkeypatch):
    monkeypatch.setenv("YOUTUBE_REQUEST_DELAY_S", "3.5")
    monkeypatch.setenv("YOUTUBE_MIN_GAP_S", "120")
    monkeypatch.setenv("YOUTUBE_GAP_JITTER_S", "0")
    monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", "12")
    monkeypatch.setenv("YOUTUBE_OFFLINE", "1")
    monkeypatch.setenv("YOUTUBE_SKIP_MANIFESTS", "true")
    settings = Settings()
    assert (settings.youtube_request_delay_s, settings.youtube_min_gap_s) == (3.5, 120)
    assert (settings.youtube_gap_jitter_s, settings.youtube_block_hours) == (0, 12)
    assert settings.youtube_offline and settings.youtube_skip_manifests


def test_build_access_passes_the_settings_on(monkeypatch):
    monkeypatch.setenv("YOUTUBE_REQUEST_DELAY_S", "7")
    monkeypatch.setenv("YOUTUBE_MIN_GAP_S", "123")
    monkeypatch.setenv("YOUTUBE_SKIP_MANIFESTS", "1")
    monkeypatch.setenv("YOUTUBE_OFFLINE", "1")
    seen: dict = {}
    monkeypatch.setattr(access_mod, "fetch_facts", lambda vid, **kwargs: seen.update(kwargs, vid=vid))
    access = build_access(Settings())
    access.fetch("abc")
    assert seen["request_delay_s"] == 7 and seen["skip_manifests"] is True
    assert isinstance(access.gate, PostgresGate) and access.gate.min_gap_s == 123
    assert access.offline is True


def test_render_saves_facts_only_in_the_idea_bucket_the_document_is_in(tmp_path):
    inbox_doc = tmp_path / "idea-bucket" / "inbox" / "clippings" / "a.md"
    inbox_doc.parent.mkdir(parents=True)
    inbox_doc.write_text("x")
    assert _facts_dir_of(inbox_doc) == (tmp_path / "idea-bucket" / "facts").resolve()
    stray = tmp_path / "somewhere" / "a.md"
    stray.parent.mkdir()
    stray.write_text("x")
    assert _facts_dir_of(stray) is None  # a stray path never writes into some repo


def test_the_run_command_has_the_youtube_flags(monkeypatch, no_run_lock):
    from typer.testing import CliRunner

    import catcher.cli as cli
    from catcher.core.config import Settings
    from catcher.modules.pipeline.report import RunReport
    from catcher.modules.worker.runner import RunOutcome

    seen: dict = {}

    def fake_run(settings, **kwargs):  # B5b: the command calls run_command (the worker path), not the loop
        seen.update(kwargs)
        return RunOutcome(report=RunReport())

    monkeypatch.setattr(cli, "run_command", fake_run)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    result = CliRunner().invoke(cli.app, ["run", "pipeline", "--refresh-facts", "--wait-youtube"])
    assert result.exit_code == 0, result.output
    assert seen["params"]["refresh_facts"] is True and seen["wait_youtube_s"] == Settings().youtube_wait_max_s
    plain = CliRunner().invoke(cli.app, ["run", "pipeline"])
    assert plain.exit_code == 0 and "refresh_facts" not in seen["params"] and seen["wait_youtube_s"] is None
