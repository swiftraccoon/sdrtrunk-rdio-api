# CLAUDE.md - AI Assistant Standards and Guidelines

## Core Principles

### 1. Professionalism and Excellence

- **NEVER** make assumptions about the codebase - ALWAYS read and verify
- **NEVER** suggest changes without understanding the full context
- **NEVER** provide incomplete or untested solutions
- **ALWAYS** maintain the highest standards of code quality and architecture

### 2. Code Quality Standards

- **STRICT** adherence to project's linting, formatting, and type checking rules
- **MANDATORY** verification that all tests pass after any change
- **REQUIRED** to run `make check` before considering any task complete
- **FORBIDDEN** to introduce code that breaks existing functionality

### 3. Codebase Reference Requirements

- **MUST** read relevant files before suggesting any changes
- **MUST** understand existing patterns and follow them exactly
- **MUST** check imports, dependencies, and configuration before adding new ones
- **NEVER** assume a library/framework is available - verify in pyproject.toml
- **NEVER** change established patterns without explicit user request

### 4. Testing and Verification

- **ALWAYS** run tests after making changes: `uv run pytest`
- **ALWAYS** run formatting: `uv run black . && uv run isort .`
- **ALWAYS** run linting: `uv run ruff check .`
- **ALWAYS** run type checking: `uv run mypy src`
- **NEVER** consider a task complete until all checks pass

### 5. Documentation Standards

- **MUST** update README.md when changing functionality
- **MUST** update docstrings when modifying functions/classes
- **MUST** ensure config.example.yaml reflects any config changes
- **MUST** keep API documentation accurate and complete
- **NEVER** leave documentation out of sync with code

### 6. Architecture Principles

- **RESPECT** separation of concerns - each module has a specific purpose
- **MAINTAIN** clean architecture boundaries:
  - `src/api/` - ONLY API endpoints and HTTP handling
  - `src/database/` - ONLY database operations and models
  - `src/models/` - ONLY data models (Pydantic/SQLAlchemy)
  - `src/utils/` - ONLY utility functions
  - `src/config.py` - ONLY configuration management
- **NEVER** mix concerns across module boundaries
- **NEVER** put business logic in API endpoints

### 7. Error Handling Standards

- **ALWAYS** handle errors gracefully with proper logging
- **ALWAYS** provide meaningful error messages
- **NEVER** expose internal details in API responses
- **NEVER** leave unhandled exceptions

### 8. Security Requirements

- **NEVER** log sensitive information (API keys, passwords, etc.)
- **NEVER** expose internal paths or system details in errors
- **ALWAYS** validate and sanitize all inputs
- **ALWAYS** use parameterized queries for database operations

### 9. Performance Considerations

- **CONSIDER** performance implications of all changes
- **AVOID** N+1 queries and unnecessary database calls
- **USE** appropriate indexes and query optimization
- **IMPLEMENT** proper connection pooling and resource management

### 10. Change Management Process

1. **READ** relevant code files first
2. **UNDERSTAND** the current implementation
3. **PLAN** changes that respect existing architecture
4. **IMPLEMENT** following all coding standards
5. **TEST** thoroughly with all quality checks
6. **VERIFY** no regressions were introduced
7. **UPDATE** all affected documentation

### 11. Communication Standards

- **BE** concise and direct in responses
- **PROVIDE** complete solutions, not partial ones
- **EXPLAIN** the reasoning behind architectural decisions
- **ADMIT** uncertainty rather than guessing

### 12. Continuous Improvement

- **LEARN** from the codebase patterns and conventions
- **SUGGEST** improvements only when they add clear value
- **MAINTAIN** backwards compatibility unless explicitly told otherwise
- **PRESERVE** existing functionality while adding new features

## Verification Checklist

Before considering ANY task complete:

- [ ] All relevant files have been read and understood
- [ ] Changes follow existing patterns and conventions
- [ ] All tests pass: `uv run pytest`
- [ ] Code is formatted: `uv run black . && uv run isort .`
- [ ] No linting errors: `uv run ruff check .`
- [ ] Type checking passes: `uv run mypy src`
- [ ] Documentation is updated and accurate
- [ ] No functionality has been broken
- [ ] Security implications have been considered
- [ ] Performance impact has been evaluated

## Project-Specific Requirements

### RdioCallsAPI Specific Standards

1. **HTTP/2 Support**: This project uses Hypercorn specifically for HTTP/2 support required by SDRTrunk. NEVER suggest HTTP/1.1 alternatives.

2. **Endpoint Stability**: The `/api/call-upload` endpoint is the standard RdioScanner protocol endpoint. NEVER change this without explicit instruction.

3. **Database Patterns**: Always use the DatabaseManager and DatabaseOperations classes. NEVER write raw SQL or bypass the abstraction layers.

4. **Configuration**: All settings MUST be configurable via config.yaml. NEVER hardcode values that should be configurable.

5. **Logging**: Use structured logging with appropriate levels. ALWAYS log errors with full context.

6. **File Handling**: Use the FileHandler class for all file operations. NEVER bypass the file handling abstraction.

## Final Reminder

Excellence is not optional. Every line of code, every suggestion, every change must meet the highest standards of quality, security, and maintainability. The codebase is a living system that must be treated with respect and care.
