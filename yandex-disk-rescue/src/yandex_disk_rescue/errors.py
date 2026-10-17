"""Error types shared by every command."""


class RescueError(Exception):
    """An expected, user-facing failure: the CLI prints its message without a traceback and exits with `exit_code`."""

    exit_code: int = 1
