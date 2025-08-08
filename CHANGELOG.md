# Changelog

All notable changes to RdioCallsAPI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Comprehensive security middleware with headers (X-Frame-Options, CSP, etc.)
- Rate limiting middleware with per-API-key and per-IP limits
- Request validation middleware with SQL injection and path traversal protection
- Enhanced input validation with Pydantic validators
- Custom exception hierarchy for better error handling
- Verbose audio filenames with metadata (system, talkgroup, frequency, source)
- CONTRIBUTING.md with detailed contribution guidelines
- SECURITY.md with security policy and best practices
- GitHub issue templates (bug report, feature request, question)
- Enhanced pull request template
- Database connection leak fixes with proper session cleanup
- Improved database connection pooling
- Comprehensive OpenAPI documentation with examples
- REST API query endpoints (/api/calls, /api/systems, /api/talkgroups)
- Filtering and pagination support for query endpoints
- Enhanced database indexes for query optimization
- Docker support with multi-stage build
- Docker Compose configuration
- Integration and performance test suites
- CI/CD pipeline with GitHub Actions
- Pre-commit hooks configuration

### Changed
- Enhanced file naming to include more metadata for better debugging
- Improved error messages and logging throughout
- Better type hints and mypy compliance
- Updated test fixtures for better isolation

### Fixed
- JSON array format handling for patches field from SDRTrunk (e.g., "[52198,52199]")
- Validation now correctly handles both comma-separated and JSON array formats

### Security
- Added comprehensive input sanitization
- Implemented request size limits (100MB default)
- Added security headers to all responses
- Enhanced CORS configuration
- SQL injection prevention in headers and inputs
- Path traversal attack prevention
- Proper error message sanitization to prevent information leakage

## [1.0.0] - 2024-12-06

### Added
- Initial release of RdioCallsAPI
- RdioScanner protocol implementation for SDRTrunk
- HTTP/2 support via Hypercorn
- SQLite database with SQLAlchemy ORM
- Comprehensive configuration system with YAML
- File storage with organization by date
- API key authentication with IP restrictions
- System-based access control
- Upload logging for security auditing
- Health check and metrics endpoints
- Comprehensive test suite
- Docker support
- CLI for configuration and testing
- Detailed documentation

### Features
- Multi-system support
- Talkgroup tracking
- Audio file validation (MP3)
- Configurable file retention
- Database cleanup utilities
- Statistics API
- Prometheus metrics support
- Structured logging with multiple outputs

## [0.9.0] - 2024-11-30 (Pre-release)

### Added
- Beta version for testing
- Core RdioScanner API endpoint
- Basic file handling
- SQLite database integration
- Configuration system
- Basic tests

## Version Guidelines

### Version Numbering
- **Major (X.0.0)**: Breaking changes to API or configuration
- **Minor (0.X.0)**: New features, backwards compatible
- **Patch (0.0.X)**: Bug fixes, security patches

### Release Process
1. Update version in `pyproject.toml`
2. Update CHANGELOG.md with release date
3. Create git tag: `git tag -a v1.0.0 -m "Release version 1.0.0"`
4. Push tag: `git push origin v1.0.0`
5. Create GitHub release with changelog excerpt

### Deprecation Policy
- Features will be deprecated with one minor version warning
- Deprecated features will be removed in next major version
- Clear deprecation warnings in logs and documentation

[Unreleased]: https://github.com/swiftraccoon/sdrtrunk-rdio-api/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/swiftraccoon/sdrtrunk-rdio-api/releases/tag/v1.0.0
[0.9.0]: https://github.com/swiftraccoon/sdrtrunk-rdio-api/releases/tag/v0.9.0