from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from catcher import __version__
from catcher.core.config import Settings
from catcher.modules.pipeline.staging import stage_inbox

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
