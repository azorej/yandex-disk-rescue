"""Command-line entry point for yandex-disk-rescue."""

import os
from typing import Annotated, Any

import typer
import typer.core

from yandex_disk_rescue import __version__, auth
from yandex_disk_rescue.errors import RescueError


class RescueErrorGroup(typer.core.TyperGroup):
    """Root command group that renders a `RescueError` from any nested command as `Error: <message>`."""

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except RescueError as err:
            typer.echo(f'Error: {err}', err=True)
            raise typer.Exit(err.exit_code) from None


app = typer.Typer(
    cls=RescueErrorGroup,
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


@auth_app.command()
def status() -> None:
    """Show where the token comes from and the Yandex account it belongs to."""
    token = auth.resolve_token()
    account = auth.fetch_account(token.value)
    if token.source is auth.TokenSource.ENV:
        typer.echo(f'Token: environment variable {auth.TOKEN_ENV_VAR}')
    else:
        typer.echo(f'Token: file {auth.token_path()}')
    typer.echo(f'Account: {account.login}')
    usage = f'Disk usage: {_format_size(account.used_space)} of {_format_size(account.total_space)} used'
    if account.total_space:
        usage += f' ({account.used_space / account.total_space:.1%})'
    typer.echo(usage)


def _format_size(size: int) -> str:
    if size < 1024:
        return f'{size} B'
    value = float(size)
    for unit in ('KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        value /= 1024
        if round(value, 1) < 1024 or unit == 'PiB':
            break
    return f'{value:.1f} {unit}'


if __name__ == '__main__':
    app()
