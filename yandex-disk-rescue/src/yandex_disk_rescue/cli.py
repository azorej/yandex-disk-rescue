"""Command-line entry point for yandex-disk-rescue."""

from typing import Annotated

import typer

from yandex_disk_rescue import __version__

app = typer.Typer(
    help='Download everything from a Yandex Disk account: resumable, verified, rate-limit aware.',
    no_args_is_help=True,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f'yandex-disk-rescue {__version__}')
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option('--version', callback=_print_version, is_eager=True, help='Show the version and exit.'),
    ] = False,
) -> None:
    """Download everything from a Yandex Disk account: resumable, verified, rate-limit aware."""


if __name__ == '__main__':
    app()
