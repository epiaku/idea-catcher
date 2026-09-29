import os
import secrets
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from catcher import __version__
from catcher.core.config import Settings
from catcher.core.log import configure_logging
from catcher.core.testdata import DEFAULT_SOURCE, DEFAULT_TARGET, TestDataError, reset_test_repos
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import UnknownProfile, load_profiles, resolve_profile
from catcher.modules.llm.service import LlmError, LlmRequest, reason
from catcher.modules.pipeline.inbox import (
    Note,
    calculated_stem,
    is_snapshot_of,
    name_title,
    read_note,
    scan_inbox,
)
from catcher.modules.pipeline.inputs import prompt_input
from catcher.modules.pipeline.process import ProcessOptions, default_services, process_note
from catcher.modules.pipeline.publish import write_page
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.youtube.facts import FactsUnavailable, fetch_facts
from catcher.modules.youtube.urls import video_id

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Idea Catcher: turns idea-bucket captures into epiaku-docs pages.",
)

IdeasOpt = Annotated[Path | None, typer.Option("--ideas", help="idea-bucket checkout (default: IDEAS_REPO)")]
DocsOpt = Annotated[Path | None, typer.Option("--docs", help="epiaku-docs checkout (default: DOCS_REPO)")]
FileOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--file",
        "-f",
        help="process only this document (file name, name without .md, or subfolder/name). Repeat for more",
    ),
]
ProfileOpt = Annotated[
    str | None,
    typer.Option(
        "--profile", "--llm-profile", help="LLM profile from profiles.yaml: notes, clippings or youtube"
    ),
]


@app.callback()
def main(
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR (default: LOG_LEVEL)")
    ] = None,
) -> None:
    load_dotenv(override=False)
    settings = Settings()
    try:
        configure_logging(log_level or settings.log_level, settings.log_file)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def scan(ideas: IdeasOpt = None, file: FileOpt = None) -> None:
    """List what is in inbox/ (class, id, duplicates) without changing anything."""
    settings = Settings()
    result = scan_inbox(ideas or settings.ideas_repo, only=file)
    winners: dict[str, Note] = {}
    for note in result.notes:
        best = winners.get(note.doc_id)
        if best is None or len(note.doc.body) > len(best.doc.body):
            winners[note.doc_id] = note
    for note in result.notes:
        winner = winners[note.doc_id]
        dup = note is not winner and is_snapshot_of(note.doc.body, winner.doc.body)
        status = f"duplicate of {winner.rel.as_posix()}" if dup else "would process"
        typer.echo(f"{status:<14} {note.doctype.name:<15} {note.doc_id:<24} <- {note.doc.fm['source_file']}")
    for artifact in result.artifacts:
        size = f"{artifact.size / 1024:,.0f} KB"
        too_big = artifact.size > settings.artifact_max_mb * 1024 * 1024
        status = "would skip" if too_big else "would copy"  # over ARTIFACT_MAX_MB it stays in inbox/
        typer.echo(f"{status:<14} {'artifact':<15} {size:<24} <- {artifact.path.name}")
    for rel, error in result.errors.items():
        typer.echo(f"{'unreadable':<14} {rel}: {error}", err=True)
    missing = [q for q in file or [] if q not in result.matched]
    for query in missing:
        typer.echo(f'{"not-found":<14} no document named "{query}" in inbox/', err=True)
    raise typer.Exit(1 if result.errors or missing else 0)


@app.command("reason")
def reason_cmd(document: Path, profile: ProfileOpt = None) -> None:
    """Run the LLM step on one document and print the validated JSON. Writes nothing."""
    settings = Settings()
    note = read_note(document)
    if note.doctype.name == "youtube":
        typer.echo("youtube notes need facts first: use `catcher render` for them.")
        raise typer.Exit(2)
    profiles = load_profiles(settings.profiles_file)
    try:
        name, _ = resolve_profile(profiles, requested=profile, class_default=note.doctype.llm_profile)
    except UnknownProfile as e:
        typer.echo(f"profile problem: {e}", err=True)
        raise typer.Exit(2) from e
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, load_tags()),
        schema_name=note.doctype.schema_name,
        profile=name,
    )
    try:
        result = reason(request, profiles=profiles, backends=lambda p: make_backend(p, settings))
    except LlmError as e:
        typer.echo(f"LLM step failed: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(result.output.model_dump_json(indent=2))
    typer.echo(
        f"profile={result.profile} backend={result.backend} model={result.model} "
        f"prompt={result.prompt_version} attempts={result.attempts} "
        f"tokens_in={result.usage.tokens_in} tokens_out={result.usage.tokens_out}",
        err=True,
    )


@app.command()
def render(
    document: Path,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
) -> None:
    """Summarize one document and write its page into the docs checkout (no inbox change, no git)."""
    settings = Settings()
    docs_repo = docs or settings.docs_repo
    note = read_note(document)
    note.name = f"{calculated_stem(str(note.doc.fm['captured']), secrets.token_hex(3), name_title(note))}.md"
    opts = ProcessOptions(profile=profile, docs_repo=docs_repo)
    try:
        processed = process_note(note, default_services(settings), opts)
    except (LlmError, UnknownProfile) as e:
        typer.echo(f"LLM step failed: {e}", err=True)
        raise typer.Exit(2) from e
    except FactsUnavailable as e:
        typer.echo(f"YouTube facts unavailable: {e}", err=True)
        raise typer.Exit(2) from e
    for problem in processed.problems:
        typer.echo(f"problem: {problem}", err=True)
    if processed.problems:
        raise typer.Exit(1)
    touched = write_page(docs_repo, note.doctype, note.doc_id, processed.filename, processed.page)
    typer.echo(f"wrote   {touched[0]}")
    for old in touched[1:]:
        typer.echo(f"removed {old}")


run_app = typer.Typer(no_args_is_help=True, help="Run a whole flow.")
app.add_typer(run_app, name="run")


@run_app.callback()
def run_group() -> None:
    """Run a whole flow."""


@run_app.command("pipeline")
def run_pipeline_cmd(
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="change no files, commit nothing")] = False,
    push: Annotated[bool, typer.Option("--push", help="push both repos (off by default)")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="process at most N notes")] = None,
    file: FileOpt = None,
) -> None:
    """Process the documents in inbox/: publish pages, file failures and duplicates, and commit."""
    settings = Settings()
    opts = RunOptions(profile=profile, dry_run=dry_run, push=push, limit=limit, only=file)
    report = run_pipeline(
        ideas or settings.ideas_repo, docs or settings.docs_repo, opts, default_services(settings)
    )
    for item in report.items:
        detail = " ".join(part for part in (item.page or "", item.message) if part)
        typer.echo(f"{item.status:<14} {item.doc_class:<15} {item.doc_id:<24} {detail}")
    for rel, error in report.unreadable.items():
        typer.echo(f"{'unreadable':<14} {rel}: {error}")
    for query in report.not_found:
        typer.echo(f'{"not-found":<14} no document named "{query}" in inbox/')
    typer.echo(f"summary: {report.counts()} committed={report.committed} pushed={report.pushed}")
    failed = bool(report.unreadable or report.not_found) or report.counts().get("failed", 0) > 0
    raise typer.Exit(1 if failed else 0)


youtube_app = typer.Typer(no_args_is_help=True, help="YouTube helpers.")
app.add_typer(youtube_app, name="youtube")


@youtube_app.callback()
def youtube_group() -> None:
    """YouTube helpers."""


@youtube_app.command("facts")
def youtube_facts(url: str) -> None:
    """Print the facts (counts, description, transcript) for one video as JSON."""
    vid = video_id(url) or url
    try:
        facts = fetch_facts(vid, languages=Settings().transcript_language_list)
    except FactsUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    typer.echo(facts.model_dump_json(indent=2))


testdata_app = typer.Typer(no_args_is_help=True, help="Test data for trying the Idea Catcher on copies.")
app.add_typer(testdata_app, name="testdata")


@testdata_app.callback()
def testdata_group() -> None:
    """Test data for trying the Idea Catcher on copies."""


@testdata_app.command("reset")
def testdata_reset(
    target: Annotated[Path, typer.Option("--target", help="where the test repos are made")] = DEFAULT_TARGET,
    source: Annotated[Path, typer.Option("--source", help="the committed test data")] = DEFAULT_SOURCE,
) -> None:
    """Delete the test repos and make fresh ones (idea-bucket, epiaku-docs) from the committed test data."""
    try:
        repos = reset_test_repos(target, source)
    except TestDataError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    for path in repos.values():
        typer.echo(f"made {path}")
    ideas, docs = (os.path.relpath(repos[name]) for name in ("idea-bucket", "epiaku-docs"))
    typer.echo(f"run on them: uv run catcher run pipeline --ideas {ideas} --docs {docs} --profile fake")
