# Example App

A simple CLI application built with Typer that demonstrates how to create command-line tools in a Python workspace. This serves as a template for building CLI applications that depend on workspace libraries.

## Features

- **Simple Commands**: Basic CLI commands with argument handling
- **Library Integration**: Shows how to use workspace dependencies
- **Type Safety**: Demonstrates proper type hints for CLI applications
- **Testing**: Includes test examples for CLI commands

### Available Commands

- `hello [NAME]`: Say hello to someone or show a generic hello message
- `greet-multiple NAMES...`: Greet multiple people at once

## Usage

```bash
# Generic hello
uv run example-app hello
# Output: Hello, World!

# Personalized greeting
uv run example-app hello Alice
# Output: Hello, Alice!

# Greet multiple people
uv run example-app greet-multiple Alice Bob Charlie
# Output:
# Hello, Alice!
# Hello, Bob!
# Hello, Charlie!

# Show help
uv run example-app --help
```

## Development

### Running from Source

```bash
# From workspace root
uv run example-app hello

# From this directory
uv run python -m example_app.main hello
```

### Testing

```bash
# Run tests for this package
uv run pytest example-app/tests/

# Run specific test
uv run pytest example-app/tests/test_main.py -v
```

## Project Structure

```
example-app/
├── src/example_app/
│   ├── __init__.py     # Package initialization
│   └── main.py         # CLI implementation
├── tests/
│   └── test_main.py    # CLI tests
├── pyproject.toml      # Package configuration
└── README.md           # This file
```

## Configuration

The application is configured in [`pyproject.toml`](pyproject.toml):

- **Entry Point**: Defined in `[project.scripts]` section
- **Dependencies**: Uses `example-library` from the workspace
- **Build System**: Uses Hatchling with src layout

## Using as Template

To adapt this for your own CLI application:

1. **Rename the package**: Update directory names and imports
2. **Update pyproject.toml**: Change name, description, and dependencies
3. **Modify commands**: Edit [`main.py`](src/example_app/main.py) to add your functionality
4. **Update tests**: Modify [`test_main.py`](tests/test_main.py) for your commands
5. **Update entry point**: Change script name in `pyproject.toml`

## Installation

This app is part of the python-template workspace and is installed automatically when you run:

```bash
uv sync
```

The CLI command `example-app` becomes available after installation.
