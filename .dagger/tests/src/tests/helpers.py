import dagger


async def expect_output(container: dagger.Container, expected: str) -> None:
    """Raise unless the container's stdout contains `expected`."""
    stdout = await container.stdout()
    if expected not in stdout:
        raise AssertionError(f'expected {expected!r} in output, got: {stdout!r}')
