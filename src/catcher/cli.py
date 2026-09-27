from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from catcher import __version__
from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import load_profiles, resolve_profile
from catcher.modules.llm.service import LlmError, LlmRequest, reason
from catcher.modules.pipeline.inputs import prompt_input
from catcher.modules.pipeline.process import ProcessOptions, default_services, process_note
from catcher.modules.pipeline.publish import write_page
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.pipeline.staging import load_staged_note, stage_inbox
from catcher.modules.pipeline.tags import load_tags

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Idea Catcher: turns idea-bucket captures into epiaku-docs pages.",
)

IdeasOpt = Annotated[Path | None, typer.Option("--ideas", help="idea-bucket checkout (default: IDEAS_REPO)")]
DocsOpt = Annotated[Path | None, typer.Option("--docs", help="epiaku-docs checkout (default: DOCS_REPO)")]
ProfileOpt = Annotated[
    str | None, typer.Option("--profile", "--llm-profile", help="LLM profile from profiles.yaml")
]


@app.callback()
def main() -> None:
    load_dotenv(override=False)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def stage(
    ideas: IdeasOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="show what would be staged")] = False,
) -> None:
    """Move inbox captures to staging/ with a stable id and class."""
    settings = Settings()
    result = stage_inbox(ideas or settings.ideas_repo, dry_run=dry_run)
    verb = "would stage" if dry_run else "staged"
    for note in result.staged:
        typer.echo(f"{verb:<12} {note.doctype.name:<15} {note.doc_id:<40} <- {note.doc.fm['source_file']}")
    for rel, error in result.errors.items():
        typer.echo(f"{'error':<12} {rel}: {error}", err=True)
    raise typer.Exit(1 if result.errors else 0)


@app.command("reason")
def reason_cmd(staged_note: Path, profile: ProfileOpt = None) -> None:
    """Run the LLM step on one staged note and print the validated JSON."""
    settings = Settings()
    note = load_staged_note(staged_note)
    if note.doctype.name in ("youtube", "youtube-gemini"):
        typer.echo("YouTube notes need facts first: use `catcher render` for them.")
        raise typer.Exit(2)
    profiles = load_profiles(settings.profiles_file)
    name, _ = resolve_profile(profiles, requested=profile, class_default=note.doctype.llm_profile)
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
    staged_note: Path,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
    no_review: Annotated[bool, typer.Option("--no-review", help="skip the YouTube reviewer")] = False,
) -> None:
    """Summarize one staged note and write its page into the docs checkout (no archive, no git)."""
    settings = Settings()
    docs_repo = docs or settings.docs_repo
    note = load_staged_note(staged_note)
    opts = ProcessOptions(profile=profile, review=not no_review, docs_repo=docs_repo)
    try:
        processed = process_note(note, default_services(settings), opts)
    except LlmError as e:
        typer.echo(f"LLM step failed: {e}", err=True)
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
    no_review: Annotated[bool, typer.Option("--no-review", help="skip the YouTube reviewer")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="change no files, commit nothing")] = False,
    push: Annotated[bool, typer.Option("--push", help="push both repos (off by default)")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="process at most N staged notes")] = None,
) -> None:
    """Stage the inbox, summarize, publish pages, archive and commit."""
    settings = Settings()
    opts = RunOptions(profile=profile, review=not no_review, dry_run=dry_run, push=push, limit=limit)
    report = run_pipeline(
        ideas or settings.ideas_repo, docs or settings.docs_repo, opts, default_services(settings)
    )
    for item in report.items:
        detail = " ".join(part for part in (item.page or "", item.message) if part)
        typer.echo(f"{item.status:<14} {item.doc_class:<15} {item.doc_id:<24} {detail}")
    for rel, error in report.staging_errors.items():
        typer.echo(f"{'stage-error':<14} {rel}: {error}")
    typer.echo(f"summary: {report.counts()} committed={report.committed} pushed={report.pushed}")
    failed = bool(report.staging_errors) or report.counts().get("failed", 0) > 0
    raise typer.Exit(1 if failed else 0)
