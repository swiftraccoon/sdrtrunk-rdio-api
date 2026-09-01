# Changelog

All notable changes to sdrtrunk-rdio-api will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Automatic retention enforcement: the server periodically deletes calls
  (audio + metadata + upload logs) older than `retention_days`, with a new
  `storage.cleanup_interval_hours` setting; stale temp files are cleaned too
- `security.trusted_proxies` setting; `X-Forwarded-For` is only honored
  when the request comes from a listed proxy (spoofing no longer bypasses
  per-key `allowed_ips` restrictions)
- `clean --yes` flag for non-interactive/cron use; `clean` output now
  covers calls, upload logs, and audio files
- Startup banner shows which config file was loaded (and warns loudly when
  it was not found)
- Comprehensive security middleware with headers (X-Frame-Options, CSP, etc.)
- Request validation middleware (request size and content-type limits)
- Enhanced input validation with Pydantic validators
- Custom exception hierarchy for better error handling
- Verbose audio filenames with metadata (system, talkgroup, frequency, source)
- CONTRIBUTING.md with detailed contribution guidelines
- SECURITY.md with security policy and best practices
- GitHub issue templates (bug report, feature request, question)
- Enhanced pull request template
- Database connection leak fixes with proper session cleanup
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

- Rate limits now come from `security.rate_limit` configuration
  (minute/hour/day) instead of hardcoded values; defaults raised to
  600/minute, 10,000/hour, 100,000/day so busy trunked systems don't lose
  calls to 429 responses
- `callId` in upload responses is now the database id, usable directly
  with `/api/calls/{id}` and `/api/calls/{id}/audio`
- SDRTrunk's "Test" button now validates the API key (test requests were
  previously accepted before authentication)
- Client input errors return 400 (or 413 for oversized audio) with a clear
  message instead of 500
- `init` writes `config/config.yaml` (the path the server actually reads);
  its template is kept in sync with `config/config.example.yaml`
- `export --end-date` is now inclusive of the end day
- `serve --api-key` appends to configured keys instead of replacing them
- Statistics, cleanup, and CLI windows consistently use UTC
- `/health` uses a cheap connectivity probe; `/metrics` derives storage
  figures from the database instead of walking the audio tree
- Query endpoints run in the threadpool so database work no longer blocks
  the event loop; upload DB/file work is offloaded the same way
- Request size limit follows `file_handling.max_file_size_mb` instead of a
  hardcoded 100 MB
- API version is sourced from package metadata (was hardcoded in three
  places with two different values)
- Frequency validation accepts HF/shortwave (below 25 MHz)
- Enhanced file naming to include more metadata for better debugging
- Improved error messages and logging throughout
- Better type hints and mypy compliance
- Updated test fixtures for better isolation

### Fixed

- `clean` crashed with `UnboundLocalError` when only database records (no
  audio files) were old enough to delete
- `clean` now deletes the audio files referenced by removed records and
  prunes empty date directories, keeping DB and filesystem consistent
- SQLite sessions now run in real transactions; `session.rollback()`
  previously had no effect because the driver was left in autocommit
- Concurrent uploads with identical metadata could silently overwrite each
  other's audio files (exclusive-create now guarantees unique names)
- API keys no longer appear in logs at DEBUG level
- Non-ASCII API keys no longer crash key comparison with a 500
- `get_systems_summary` issued one query per system (N+1); now a single
  window-function query
- JSON array format handling for patches field from SDRTrunk (e.g., "[52198,52199]")
- Validation now correctly handles both comma-separated and JSON array formats

### Removed

- Configuration options that nothing implemented: `database.pool_size`,
  `database.max_overflow`, `processing.store_fields`,
  `monitoring.statistics.*` (stale keys in existing configs are ignored)
- Dead code: `CORSSecurityMiddleware`, the never-written `SystemStats`
  table, SQL-injection header heuristics (false positives on legitimate
  keys; real protection is the ORM's parameterized queries),
  `sanitize_string`, and the deprecated `X-XSS-Protection` header
- Unused ffmpeg from the Docker runtime image
- `scripts/docker-compose.yml` (referenced unsupported `RDIO_*`
  environment variables)

### Security

- Config files that exist but fail to parse or validate now abort startup
  instead of silently running with defaults (which meant open access)
- CORS no longer combines wildcard origins with credentials
- Filename sanitization applied to uploaded filenames before temp storage
- Request size limits tied to configuration
- Security headers on all responses
- Path traversal protection on audio file serving and stored-file deletion
- Proper error message sanitization to prevent information leakage

## [1.0.0] - 2024-12-06

### Added

- Initial release of sdrtrunk-rdio-api
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
