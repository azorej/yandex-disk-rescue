"""Failures of Yandex Disk API requests."""

from yandex_disk_rescue.errors import RescueError


class TokenRejectedError(RescueError):
    """The API answered 401 or 403: the token is invalid, expired, revoked or lacks Disk access."""


class DiskApiError(RescueError):
    """Any other failed request: an error status, a transport error or an unexpected response body. `reason` is the
    failure itself, and `context` what the request was for (`Could not download disk:/a.jpg`), if anything; the
    message is `<context>: <reason>`, or the reason alone."""

    def __init__(self, reason: str, *, context: str | None = None) -> None:
        super().__init__(f'{context}: {reason}' if context else reason)
        self.reason = reason
        self.context = context


class DownloadRefusedError(RescueError):
    """The API refused a download link for one file (a non-throttling 4xx other than 401 or 404); `reason` is the
    status and the API's description."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f'Could not download {path}: {reason}')
        self.reason = reason


class PreviewRefusedError(RescueError):
    """A preview link was not fetched, or its answer was not a usable preview; the message is the reason, which
    never quotes the link."""


class RemotePathNotFoundError(RescueError):
    pass


class NotAFolderError(RescueError):
    def __init__(self, path: str) -> None:
        super().__init__(f"Remote path '{path}' is a file, not a folder.")


class UnsupportedPathError(RescueError):
    exit_code = 2
