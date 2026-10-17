"""Command-line entry point for yandex-disk-rescue."""

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import typer
import typer.core

from yandex_disk_rescue import __version__, api, auth, config, settings
from yandex_disk_rescue.commands import scan
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.units import format_size


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
app.command()(scan.scan)

REVOKE_URL = 'https://id.yandex.com/personal/data-access'


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f'yandex-disk-rescue {__version__}')
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option(
            '--config',
            envvar=config.CONFIG_ENV_VAR,
            metavar='PATH',
            help='Config file with option values (default: $XDG_CONFIG_HOME/yandex-disk-rescue/config.toml).',
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option('--version', callback=_print_version, is_eager=True, help='Show the version and exit.'),
    ] = False,
) -> None:
    """Download everything from a Yandex Disk account: resumable, verified, rate-limit aware."""
    values = config.load_config(config_path)
    ctx.default_map = _default_map(ctx.command, {settings.SETTINGS[name].flag: value for name, value in values.items()})


def _default_map(command: typer.core.TyperCommand | typer.core.TyperGroup, by_flag: dict[str, Any]) -> dict[str, Any]:
    """Click's `default_map` for `command` and its subcommands: the config-file value of each registry option it takes.

    Click uses a `default_map` value when neither the flag nor the env var is given, and still runs the option's
    validating callback on it.
    """
    default_map = {param.name: by_flag[flag] for param in command.params for flag in param.opts if flag in by_flag}
    if isinstance(command, typer.core.TyperGroup):
        for name, subcommand in command.commands.items():
            default_map[name] = _default_map(subcommand, by_flag)
    return default_map


@auth_app.command()
def login(
    client_id: settings.ClientId = settings.CLIENT_ID.default,
    api_interval_ms: settings.ApiIntervalMs = settings.API_INTERVAL_MS.default,
    api_concurrency: settings.ApiConcurrency = settings.API_CONCURRENCY.default,
    api_timeout: settings.ApiTimeout = settings.API_TIMEOUT.default,
    api_retries: settings.ApiRetries = settings.API_RETRIES.default,
    api_url: settings.ApiUrl = settings.API_URL.default,
) -> None:
    """Get a token in the browser, check it against the Disk API and store it."""
    typer.echo('Open this URL in a browser, sign in to Yandex and allow access:', err=True)
    typer.echo('', err=True)
    typer.echo(f'  {auth.authorize_url(client_id)}', err=True)
    typer.echo('', err=True)
    typer.echo('Yandex then shows a token. Copy it (or the whole address-bar URL) and paste it here.', err=True)
    token = auth.token_from_paste(typer.prompt('Paste the token', hide_input=True, err=True))
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    account = asyncio.run(_account_of(token, base_url=api_url, limits=limits))
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
def status(
    api_interval_ms: settings.ApiIntervalMs = settings.API_INTERVAL_MS.default,
    api_concurrency: settings.ApiConcurrency = settings.API_CONCURRENCY.default,
    api_timeout: settings.ApiTimeout = settings.API_TIMEOUT.default,
    api_retries: settings.ApiRetries = settings.API_RETRIES.default,
    api_url: settings.ApiUrl = settings.API_URL.default,
) -> None:
    """Show where the token comes from and the Yandex account it belongs to."""
    token = auth.resolve_token()
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    account = asyncio.run(_account_of(token.value, base_url=api_url, limits=limits))
    if token.source is auth.TokenSource.ENV:
        typer.echo(f'Token: environment variable {auth.TOKEN_ENV_VAR}')
    else:
        typer.echo(f'Token: file {auth.token_path()}')
    typer.echo(f'Account: {account.login}')
    usage = f'Disk usage: {format_size(account.used_space)} of {format_size(account.total_space)} used'
    if account.total_space:
        usage += f' ({account.used_space / account.total_space:.1%})'
    typer.echo(usage)


async def _account_of(token: str, *, base_url: str, limits: api.ApiLimits) -> api.Account:
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        return await client.account()


if __name__ == '__main__':
    app()
