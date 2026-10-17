"""Command-line entry point for yandex-disk-rescue."""

import os
from typing import Annotated

import typer

from yandex_disk_rescue import __version__, auth

app = typer.Typer(
    help='Download everything from a Yandex Disk account: resumable, verified, rate-limit aware.',
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
auth_app = typer.Typer(help='Manage the Yandex OAuth token.', no_args_is_help=True)
app.add_typer(auth_app, name='auth')

REVOKE_URL = 'https://id.yandex.com/personal/data-access'


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


@auth_app.command()
def logout() -> None:
    """Delete the stored token."""
    path = auth.token_path()
    if auth.delete_token():
        typer.echo(f'Removed the stored token {path}.')
    else:
        typer.echo(f'No stored token at {path}.')
    typer.echo(f"Also revoke this app's access in Yandex ID: {REVOKE_URL}")
    if auth.TOKEN_ENV_VAR in os.environ:
        typer.echo(f'Warning: {auth.TOKEN_ENV_VAR} is set and still provides a token.', err=True)


if __name__ == '__main__':
    app()
