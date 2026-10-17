"""The registry of options: every option's name, flag, env var, default and validation.

A command takes an option by annotating a parameter with the entry's `Annotated` type and defaulting it to
the entry's `default`. A value comes from the flag, else the env var, else the config file, else the default.
Validation runs on the value from every source; a violation is a usage error (exit 2) naming the option and the
values it accepts.
"""

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import typer

from yandex_disk_rescue.api import ApiLimits

ENV_VAR_PREFIX = 'YANDEX_DISK_RESCUE_'


@dataclass(frozen=True)
class Setting[T]:
    """One option. `name` is also its config-file key; the flag and env var are derived from it.

    `check` returns why a value is invalid, phrased to follow "must", or None for a valid value.
    """

    name: str
    default: T
    help: str
    check: Callable[[T], str | None]
    hidden: bool = False

    @property
    def flag(self) -> str:
        return '--' + self.name.replace('_', '-')

    @property
    def env_var(self) -> str:
        return ENV_VAR_PREFIX + self.name.upper()

    def reason_invalid(self, value: object) -> str | None:
        """Why `value`, of any type (as read from the config file), is not valid for this option, or None."""
        expected = type(self.default)
        if type(value) is not expected:
            return f'must be {_TYPE_NAMES[expected]}.'
        return self.check(value)

    def typer_option(self) -> Any:
        return typer.Option(
            self.flag, envvar=self.env_var, callback=self._validated, help=self.help, hidden=self.hidden
        )

    def _validated(self, value: T) -> T:
        if (reason := self.check(value)) is not None:
            raise typer.BadParameter(reason)
        return value


_TYPE_NAMES = {int: 'an integer', str: 'a string'}


def _in_range(low: int, high: int) -> Callable[[int], str | None]:
    def check(value: int) -> str | None:
        return None if low <= value <= high else f'must be an integer from {low} to {high}.'

    return check


def _http_url(value: str) -> str | None:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme in ('http', 'https') and parts.netloc:
        return None
    return 'must be an http:// or https:// URL.'


def _client_id(value: str) -> str | None:
    if re.fullmatch(r'[0-9a-f]{32}', value):
        return None
    return 'must be 32 lowercase hexadecimal characters (the ClientID of a Yandex OAuth app).'


API_INTERVAL_MS = Setting(
    'api_interval_ms',
    50,
    'Minimum gap between the starts of two Disk API requests, in milliseconds.',
    _in_range(25, 1000),
)
API_CONCURRENCY = Setting('api_concurrency', 7, 'Maximum number of Disk API requests in flight.', _in_range(1, 30))
API_TIMEOUT = Setting('api_timeout', 30, 'Timeout of one Disk API request, in seconds.', _in_range(1, 300))
API_RETRIES = Setting(
    'api_retries', 5, 'Retries of a Disk API request after a transport error or a throttling status.', _in_range(0, 20)
)
API_URL = Setting('api_url', 'https://cloud-api.yandex.net', 'Base URL of the Disk REST API.', _http_url, hidden=True)
DOWNLOAD_TIMEOUT = Setting(
    'download_timeout',
    15,
    'Stall timeout: a download fails as stalled when no data arrives from the download host for this long, in seconds.',
    _in_range(1, 600),
)
DOWNLOAD_RETRIES = Setting(
    'download_retries',
    5,
    'Retries of a download, at once and from where it stopped, after it failed having received new data.',
    _in_range(0, 20),
)
DOWNLOAD_PASSES = Setting(
    'download_passes',
    7,
    'Passes over the files to download: each pass after the first resumes the files whose download failed '
    'transiently (a stall, a connection error, a 5xx); 0 passes until every file is done.',
    _in_range(0, 1000),
)
SCAN_MAX_AGE = Setting(
    'scan_max_age',
    60,
    'Reuse the last completed scan covering the remote folder when it is younger than this, in minutes; 0 always '
    'scans.',
    _in_range(0, 10080),
)
CLIENT_ID = Setting(
    'client_id',
    '9e2a0382c430429c9d57913b61d7d48f',
    'ClientID of the Yandex OAuth app to authorize; the default is the built-in app.',
    _client_id,
)

SETTINGS: dict[str, Setting[Any]] = {
    setting.name: setting
    for setting in (
        API_INTERVAL_MS,
        API_CONCURRENCY,
        API_TIMEOUT,
        API_RETRIES,
        API_URL,
        DOWNLOAD_TIMEOUT,
        DOWNLOAD_RETRIES,
        DOWNLOAD_PASSES,
        SCAN_MAX_AGE,
        CLIENT_ID,
    )
}

ApiIntervalMs = Annotated[int, API_INTERVAL_MS.typer_option()]
ApiConcurrency = Annotated[int, API_CONCURRENCY.typer_option()]
ApiTimeout = Annotated[int, API_TIMEOUT.typer_option()]
ApiRetries = Annotated[int, API_RETRIES.typer_option()]
ApiUrl = Annotated[str, API_URL.typer_option()]
DownloadTimeout = Annotated[int, DOWNLOAD_TIMEOUT.typer_option()]
DownloadRetries = Annotated[int, DOWNLOAD_RETRIES.typer_option()]
DownloadPasses = Annotated[int, DOWNLOAD_PASSES.typer_option()]
ScanMaxAge = Annotated[int, SCAN_MAX_AGE.typer_option()]
ClientId = Annotated[str, CLIENT_ID.typer_option()]


def api_limits(*, interval_ms: int, concurrency: int, timeout: int, retries: int) -> ApiLimits:
    return ApiLimits(interval=interval_ms / 1000, concurrency=concurrency, timeout=timeout, retries=retries)
