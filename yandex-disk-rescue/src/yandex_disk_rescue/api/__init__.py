"""The Yandex Disk REST API client (account check, folder listing, download links, error mapping, pacing and
retries) and the content client that fetches file bytes from the download host."""

from yandex_disk_rescue.api.client import ApiLimits, DiskClient
from yandex_disk_rescue.api.errors import (
    DiskApiError,
    DownloadRefusedError,
    NotAFolderError,
    RemotePathNotFoundError,
    TokenRejectedError,
    UnsupportedPathError,
)
from yandex_disk_rescue.api.limiter import MAX_PAUSE_SECONDS, AdaptiveLimiter, parse_retry_after
from yandex_disk_rescue.api.models import Account, Resource
from yandex_disk_rescue.api.remote_paths import ROOT, normalize_remote_path
from yandex_disk_rescue.api.transfer import (
    ContentClient,
    LinkExpiredError,
    RangeNotHonouredError,
    TooManyBytesError,
    TransferError,
    TransferRefusedError,
    TransientTransferError,
)
from yandex_disk_rescue.api.user_agent import USER_AGENT

__all__ = [
    'MAX_PAUSE_SECONDS',
    'ROOT',
    'USER_AGENT',
    'Account',
    'AdaptiveLimiter',
    'ApiLimits',
    'ContentClient',
    'DiskApiError',
    'DiskClient',
    'DownloadRefusedError',
    'LinkExpiredError',
    'NotAFolderError',
    'RangeNotHonouredError',
    'RemotePathNotFoundError',
    'Resource',
    'TokenRejectedError',
    'TooManyBytesError',
    'TransferError',
    'TransferRefusedError',
    'TransientTransferError',
    'UnsupportedPathError',
    'normalize_remote_path',
    'parse_retry_after',
]
