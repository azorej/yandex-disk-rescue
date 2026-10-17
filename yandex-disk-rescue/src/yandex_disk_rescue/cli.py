"""Command-line entry point for yandex-disk-rescue."""

import os
import re
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
CLIENT_ID_PATTERN = re.compile(r'[0-9a-f]{32}')


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


def _validate_client_id(value: str) -> str:
    if not CLIENT_ID_PATTERN.fullmatch(value):
        raise typer.BadParameter('must be 32 lowercase hexadecimal characters (the ClientID of a Yandex OAuth app).')
    return value


@auth_app.command()
def login(
    client_id: Annotated[
        str,
        typer.Option(
            envvar=auth.CLIENT_ID_ENV_VAR,
            callback=_validate_client_id,
            help='ClientID of the Yandex OAuth app to authorize; the default is the built-in app.',
        ),
    ] = auth.DEFAULT_CLIENT_ID,
) -> None:
    """Get a token in the browser, check it against the Disk API and store it."""
    typer.echo('Open this URL in a browser, sign in to Yandex and allow access:', err=True)
    typer.echo('', err=True)
    typer.echo(f'  {auth.authorize_url(client_id)}', err=True)
    typer.echo('', err=True)
    typer.echo('Yandex then shows a token. Copy it (or the whole address-bar URL) and paste it here.', err=True)
    token = auth.token_from_paste(typer.prompt('Paste the token', hide_input=True, err=True))
    account = auth.fetch_account(token)
    path = auth.save_token(token)
    typer.echo(f'Logged in as {account.login}. Token saved to {path}.')
    if auth.TOKEN_ENV_VAR in os.environ:
        typer.echo(f'Warning: {auth.TOKEN_ENV_VAR} is set and overrides the saved token.', err=True)


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
