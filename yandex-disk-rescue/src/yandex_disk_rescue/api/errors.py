"""Failures of Yandex Disk API requests."""

from yandex_disk_rescue.errors import RescueError


class TokenRejectedError(RescueError):
    """The API answered 401 or 403: the token is invalid, expired, revoked or lacks Disk access."""


class DiskApiError(RescueError):
    """Any other failed request: an error status, a transport error or an unexpected response body."""


class RemotePathNotFoundError(RescueError):
    pass


class NotAFolderError(RescueError):
    pass


class UnsupportedPathError(RescueError):
    exit_code = 2
