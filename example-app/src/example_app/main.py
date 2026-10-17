"""Main CLI module for example-app."""

from typing import Annotated

import typer
from example_library import greet, say_hello

app = typer.Typer(help='Example CLI app using Typer and example-library')


@app.command()
def hello(
    name: Annotated[str | None, typer.Argument(help='Name to greet. If not provided, shows generic hello.')] = None,
) -> None:
    """Say hello to someone or show a generic hello message."""
    if name is None:
        typer.echo(say_hello())
        return
    if not name.strip():
        typer.echo('Error: Name cannot be empty')
        raise typer.Exit(1)
    typer.echo(greet(name))


@app.command()
def greet_multiple(names: Annotated[list[str], typer.Argument(help='Names to greet')]) -> None:
    """Greet multiple people, reporting (and skipping) empty names."""
    for name in names:
        if not name.strip():
            typer.echo(f'Error greeting {name}: Name cannot be empty')
            continue
        typer.echo(greet(name))


if __name__ == '__main__':
    app()
