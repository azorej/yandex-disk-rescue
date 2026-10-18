from collections.abc import Awaitable

import dagger


async def expect_output(container: dagger.Container, expected: str) -> None:
    """Raise unless the container's stdout contains `expected`."""
    stdout = await container.stdout()
    if expected not in stdout:
        raise AssertionError(f'expected {expected!r} in output, got: {stdout!r}')


async def expect_error(call: Awaitable[object], expected: str) -> None:
    """Raise unless awaiting `call` fails with an error whose message contains `expected`."""
    try:
        result = await call
    except dagger.QueryError as error:
        if expected not in str(error):
            raise AssertionError(f'expected {expected!r} in the error, got: {error}') from error
        return
    raise AssertionError(f'expected an error with {expected!r}, got: {result!r}')
