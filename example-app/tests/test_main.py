"""Tests for the main CLI module."""

from example_app.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_hello_without_name():
    """Test the hello command without providing a name."""
    result = runner.invoke(app, ['hello'])
    assert result.exit_code == 0
    assert 'Hello, World!' in result.stdout


def test_hello_with_name():
    """Test the hello command with a name."""
    result = runner.invoke(app, ['hello', 'Alice'])
    assert result.exit_code == 0
    assert 'Hello, Alice!' in result.stdout


def test_hello_with_empty_name():
    """Test the hello command with an empty name."""
    result = runner.invoke(app, ['hello', ''])
    assert result.exit_code == 1
    assert 'Error: Name cannot be empty' in result.stdout


def test_greet_multiple():
    """Test the greet-multiple command."""
    result = runner.invoke(app, ['greet-multiple', 'Alice', 'Bob', 'Charlie'])
    assert result.exit_code == 0
    assert 'Hello, Alice!' in result.stdout
    assert 'Hello, Bob!' in result.stdout
    assert 'Hello, Charlie!' in result.stdout


def test_greet_multiple_with_empty_name():
    """Test the greet-multiple command with an empty name."""
    result = runner.invoke(app, ['greet-multiple', 'Alice', '', 'Bob'])
    assert result.exit_code == 0
    assert 'Hello, Alice!' in result.stdout
    assert 'Error greeting : Name cannot be empty' in result.stdout
    assert 'Hello, Bob!' in result.stdout


def test_app_help():
    """Test the app help command."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    assert 'Example CLI app using Typer and example-library' in result.stdout
