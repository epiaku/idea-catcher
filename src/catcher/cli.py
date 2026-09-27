from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from catcher import __version__

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
