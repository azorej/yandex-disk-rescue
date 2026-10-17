"""Failures of Yandex Disk API requests."""

from yandex_disk_rescue.errors import RescueError


class TokenRejectedError(RescueError):
    """The API answered 401 or 403: the token is invalid, expired, revoked or lacks Disk access."""


class DiskApiError(RescueError):
    """Any other failed request: an error status, a transport error or an unexpected response body."""


class DownloadRefusedError(RescueError):
    """The API refused a download link for one file (a non-throttling 4xx other than 401 or 404); `reason` is the
    status and the API's description."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f'Could not download {path}: {reason}')
        self.reason = reason


class RemotePathNotFoundError(RescueError):
    pass


class NotAFolderError(RescueError):
    def __init__(self, path: str) -> None:
        super().__init__(f"Remote path '{path}' is a file, not a folder.")


class UnsupportedPathError(RescueError):
    exit_code = 2
