# Example Library

A simple Python library demonstrating best practices for package structure, testing, and workspace integration. This serves as a template for creating reusable Python libraries in a workspace environment.

## Features

- **Simple API**: Clean, documented functions for common operations
- **Type Safety**: Full type hints for better IDE support and code quality
- **Comprehensive Testing**: Example test suite with pytest
- **Workspace Integration**: Proper workspace dependency configuration

### Available Functions

- `say_hello()`: Returns a simple "Hello, World!" message
- `greet(name)`: Returns a personalized greeting for the given name

## Usage

```python
from example_library import say_hello, greet

# Basic hello
message = say_hello()
print(message)  # Hello, World!

# Personalized greeting
greeting = greet('Alice')
print(greeting)  # Hello, Alice!
```

### Advanced Usage

```python
from example_library.hello import say_hello, greet

# You can also import directly from the module
names = ['Alice', 'Bob', 'Charlie']
greetings = [greet(name) for name in names]

for greeting in greetings:
    print(greeting)
```

## Development

### Testing

```bash
# Run tests for this package
uv run pytest example-library/tests/

# Run with coverage
uv run pytest example-library/tests/ --cov=example_library

# Run specific test
uv run pytest example-library/tests/test_hello.py::test_say_hello -v
```

### Code Quality

Linting and formatting are repo-wide: run `mise run lint` or `mise run format` from the workspace root.

## Project Structure

```
example-library/
├── src/example_library/
│   ├── __init__.py     # Package initialization with exports
│   └── hello.py        # Main module with functions
├── tests/
│   └── test_hello.py   # Test suite
├── pyproject.toml      # Package configuration
└── README.md           # This file
```

## Configuration

The library is configured in [`pyproject.toml`](pyproject.toml):

- **Build System**: Uses Hatchling with src layout
- **Dependencies**: Minimal runtime dependencies
- **Package Discovery**: Automatic package discovery from src/
- **Version**: Semantic versioning

## Using as Template

To adapt this for your own library:

1. **Rename the package**:
   - Update directory: `src/example_library/` → `src/your_library/`
   - Update imports in `__init__.py` and tests

2. **Update pyproject.toml**:
   - Change `name = "example-library"` to your package name
   - Update description, author, and other metadata
   - Modify dependencies as needed

3. **Implement your functionality**:
   - Replace functions in [`hello.py`](src/example_library/hello.py)
   - Add new modules as needed
   - Export public API in [`__init__.py`](src/example_library/__init__.py)

4. **Update tests**:
   - Modify [`test_hello.py`](tests/test_hello.py) for your functions
   - Add new test files for additional modules

5. **Update workspace configuration**:
   - Add your library to workspace `pyproject.toml`
   - Update any dependent packages

## API Reference

### `say_hello() -> str`

Returns a generic greeting message.

**Returns:**
- `str`: The greeting message "Hello, World!"

**Example:**
```python
>>> from example_library import say_hello
>>> say_hello()
'Hello, World!'
```

### `greet(name: str) -> str`

Returns a personalized greeting for the specified name.

**Parameters:**
- `name` (str): The name of the person to greet

**Returns:**
- `str`: A personalized greeting message

**Example:**
```python
>>> from example_library import greet
>>> greet("Alice")
'Hello, Alice!'
```

## Installation

This library is part of the python-template workspace and is installed automatically when you run:

```bash
uv sync
```
