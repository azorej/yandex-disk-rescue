"""Hello world functionality for the example library."""


def say_hello() -> str:
    """Return a simple hello world message.

    Returns:
        str: A hello world message.
    """
    return 'Hello, World!'


def greet(name: str) -> str:
    """Return a personalized greeting.

    Args:
        name: The name to greet.

    Returns:
        str: A personalized greeting message.
    """
    if not name:
        raise ValueError('Name cannot be empty')
    return f'Hello, {name}!'
