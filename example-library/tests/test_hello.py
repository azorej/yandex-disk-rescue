"""Tests for the hello module."""

import pytest
from example_library.hello import greet, say_hello


def test_say_hello():
    """Test the say_hello function."""
    result = say_hello()
    assert result == 'Hello, World!'


def test_greet_with_name():
    """Test the greet function with a valid name."""
    result = greet('Alice')
    assert result == 'Hello, Alice!'


def test_greet_with_empty_name():
    """Test the greet function with an empty name."""
    with pytest.raises(ValueError, match='Name cannot be empty'):
        greet('')


def test_greet_with_none():
    """Test the greet function with None."""
    with pytest.raises(ValueError, match='Name cannot be empty'):
        greet(None)


def test_greet_with_spaces():
    """Test the greet function with a name containing spaces."""
    result = greet('John Doe')
    assert result == 'Hello, John Doe!'
