# Contributing to sdrtrunk-rdio-api

Thank you for your interest in contributing to sdrtrunk-rdio-api! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Coding Standards](#coding-standards)
- [Testing Requirements](#testing-requirements)
- [Pull Request Process](#pull-request-process)
- [Issue Guidelines](#issue-guidelines)
- [Documentation](#documentation)
- [Community](#community)

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct:

- **Be respectful**: Treat everyone with respect. No harassment, discrimination, or inappropriate behavior.
- **Be collaborative**: Work together to resolve conflicts and find solutions.
- **Be inclusive**: Welcome newcomers and help them get started.
- **Be professional**: Keep discussions focused on the project and technical matters.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/sdrtrunk-rdio-api.git
   cd sdrtrunk-rdio-api
   ```
3. **Add the upstream repository**:
   ```bash
   git remote add upstream https://github.com/swiftraccoon/sdrtrunk-rdio-api.git
   ```

## Development Setup

### Prerequisites

- Python 3.13 or higher
- [uv](https://github.com/astral-sh/uv) package manager
- Git

### Installation

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Install dependencies**:
   ```bash
   uv sync
   ```

3. **Copy and configure the config file**:
   ```bash
   cp config/config.example.yaml config/config.yaml
   # Edit config/config.yaml as needed for your setup
   ```

4. **Run tests to verify setup**:
   ```bash
   make test
   ```

### Development Workflow

1. **Create a new branch** for your feature/fix:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following our coding standards

3. **Run quality checks**:
   ```bash
   make check  # Runs all checks (format, lint, type, test)
   ```

4. **Commit your changes** with clear, descriptive messages:
   ```bash
   git add .
   git commit -m "feat: add new feature X"
   ```

## How to Contribute

### Types of Contributions

- **Bug Reports**: Report issues you encounter
- **Bug Fixes**: Submit PRs to fix existing issues
- **Features**: Propose and implement new features
- **Documentation**: Improve or add documentation
- **Tests**: Add missing tests or improve test coverage
- **Performance**: Optimize code for better performance
- **Refactoring**: Improve code quality and maintainability

### Before Starting Work

1. **Check existing issues** to avoid duplicate work
2. **Discuss major changes** by opening an issue first
3. **Claim an issue** by commenting on it if you want to work on it

## Coding Standards

### Python Style Guide

- Follow [PEP 8](https://pep8.org/) with these tools:
  - **Black** for formatting
  - **isort** for import sorting
  - **Ruff** for linting
  - **mypy** for type checking

### Code Quality Requirements

1. **Type Hints**: All functions must have type hints
2. **Docstrings**: All public functions/classes need docstrings
3. **Comments**: Add comments for complex logic
4. **No hardcoded values**: Use configuration or constants
5. **Error handling**: Properly handle exceptions with logging

### Example Function

```python
def process_audio_file(
    file_path: Path,
    config: Config,
    validate: bool = True
) -> ProcessResult:
    """Process an audio file for storage.
    
    Args:
        file_path: Path to the audio file
        config: Application configuration
        validate: Whether to validate the file
        
    Returns:
        ProcessResult with status and metadata
        
    Raises:
        InvalidAudioFormatError: If audio format is invalid
        FileSizeError: If file exceeds size limits
    """
    logger.info(f"Processing audio file: {file_path}")
    
    try:
        # Implementation here
        pass
    except Exception as e:
        logger.error(f"Failed to process audio: {e}")
        raise
```

## Testing Requirements

### Test Coverage

- **Minimum coverage**: 80% for new code
- **Target coverage**: 90% overall
- **Required tests**:
  - Unit tests for all new functions
  - Integration tests for API endpoints
  - Error path testing

### Writing Tests

```python
# tests/test_your_feature.py
import pytest
from your_module import your_function

def test_your_function_success():
    """Test successful case."""
    result = your_function(valid_input)
    assert result.status == "success"

def test_your_function_error():
    """Test error handling."""
    with pytest.raises(ExpectedException):
        your_function(invalid_input)
```

### Running Tests

```bash
# Run all tests
make test

# Run with coverage
make test-coverage

# Run specific test file
uv run pytest tests/test_your_feature.py -xvs
```

## Pull Request Process

### Before Submitting

1. **Update from upstream**:
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run all checks**:
   ```bash
   make check
   ```

3. **Update documentation** if needed

4. **Add tests** for new functionality

### PR Requirements

- **Clear title**: Use conventional commits format
  - `feat:` New feature
  - `fix:` Bug fix
  - `docs:` Documentation
  - `test:` Tests
  - `refactor:` Code refactoring
  - `perf:` Performance improvements
  - `chore:` Maintenance tasks

- **Description**: Include:
  - What changed and why
  - Related issue numbers
  - Testing performed
  - Breaking changes (if any)

- **Small, focused changes**: One feature/fix per PR

- **Pass all CI checks**: All tests and quality checks must pass

### Review Process

1. **Automated checks** run on all PRs
2. **Code review** by maintainers
3. **Address feedback** promptly
4. **Merge** once approved

## Issue Guidelines

### Bug Reports

Include:
- **Description**: Clear description of the bug
- **Steps to reproduce**: Detailed steps
- **Expected behavior**: What should happen
- **Actual behavior**: What actually happens
- **Environment**: Python version, OS, config
- **Logs**: Relevant error messages

### Feature Requests

Include:
- **Use case**: Why is this needed?
- **Proposed solution**: How should it work?
- **Alternatives**: Other approaches considered
- **Impact**: Breaking changes or compatibility

## Documentation

### Types of Documentation

1. **Code documentation**: Docstrings and comments
2. **API documentation**: Endpoint descriptions
3. **Configuration docs**: Config file explanations
4. **User guides**: How-to documentation
5. **Architecture docs**: System design and decisions

### Documentation Standards

- Use clear, concise language
- Include code examples
- Keep documentation up-to-date
- Test code examples

## Community

### Getting Help

- **Issues**: Open an issue for bugs or questions
- **Discussions**: Use GitHub Discussions for general topics
- **Documentation**: Check the README and docs first

### Staying Updated

- Watch the repository for updates
- Follow release notes
- Join discussions on proposed changes

## Development Tips

### Useful Commands

```bash
# Format code
make format

# Run linter
make lint

# Type checking
make type-check

# Run specific test
uv run pytest tests/test_file.py::test_function -xvs

# Check test coverage
make test-coverage

# Clean up
make clean

# Full check before PR
make check
```

### Debugging

1. **Enable debug logging**:
   ```yaml
   # config/config.yaml
   logging:
     level: DEBUG
   ```

2. **Use debugger**:
   ```python
   import pdb; pdb.set_trace()
   ```

3. **Check logs**:
   ```bash
   tail -f logs/rdio_calls_api.log
   ```

## Recognition

Contributors will be recognized in:
- The project README
- Release notes
- GitHub contributors page

Thank you for contributing to sdrtrunk-rdio-api! Your efforts help make this project better for everyone.

## Questions?

If you have questions not covered here:
1. Check existing issues and discussions
2. Open a new discussion
3. Contact the maintainers

We're here to help and appreciate your contributions!