# RdioCallsAPI

A lightweight, modular API server for ingesting radio calls from SDRTrunk via the RdioScanner protocol.

## Features

- **RdioScanner Protocol Compatible**: Full compatibility with SDRTrunk's RdioScanner upload format
- **Modular Design**: Easy to customize and extend for your specific needs
- **Multiple Storage Options**: 
  - Filesystem storage with automatic organization
  - SQLite database for metadata
  - Option to discard audio files (metadata only)
- **Security Features**:
  - API key authentication with IP and system restrictions
  - Rate limiting
  - Request logging and audit trail
- **Monitoring**:
  - Health check endpoint
  - Statistics and metrics
  - Configurable logging
- **Performance**:
  - HTTP/2 support for SDRTrunk compatibility
  - SQLite with WAL mode for concurrent access
  - Connection pooling
  - Efficient file handling

## Quick Start

### Installation

1. Clone the repository
2. Install uv package manager:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. Install dependencies:
```bash
uv sync
```

4. Copy and customize the configuration:
```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your settings
```

5. Run the server:
```bash
uv run python main.py serve
```

The API will be available at `http://localhost:8080` with HTTP/2 support (required for SDRTrunk).

### CLI Usage

The enhanced CLI provides multiple commands for managing the server:

```bash
# Start server with default config
uv run python main.py serve

# Start server with custom settings
uv run python main.py serve --port 8080 --host 0.0.0.0

# Generate example configuration
uv run python main.py init

# View recent calls and statistics
uv run python main.py stats --last 20
uv run python main.py stats --system 123 --hours 24

# Test database connection
uv run python main.py test-db

# Clean old data
uv run python main.py clean --days 30 --dry-run
uv run python main.py clean --days 30

# Export calls to CSV
uv run python main.py export -o calls.csv --start-date 2024-01-01
```

## Configuration

The server is configured via YAML file. See `config.yaml` for all available options.

### Key Configuration Options

#### API Security
```yaml
security:
  api_keys:
    - key: "your-secret-key"
      description: "Main SDRTrunk node"
      allowed_ips: ["192.168.1.100"]  # Optional IP restriction
      allowed_systems: ["1", "2"]      # Optional system restriction
```

#### Storage Options
```yaml
file_handling:
  storage:
    strategy: "filesystem"  # Options: discard, filesystem, database
    directory: "data/audio"
    organize_by_date: true
    retention_days: 30  # 0 = keep forever
```

#### Processing Modes
```yaml
processing:
  mode: "store"  # Options: log_only, store, process
```

- `log_only`: Log metadata only, discard audio files
- `store`: Store audio files and metadata
- `process`: Store and enable for future processing hooks

## API Documentation

### RdioScanner Upload Endpoint

**POST** `/api/call-upload`

Accepts multipart form data with the following fields:

#### Required Fields
- `key` (string): API key for authentication
- `system` (string): System ID
- `dateTime` (integer): Unix timestamp in seconds
- `audio` (file): MP3 audio file

#### Optional Fields
- `frequency` (integer): Frequency in Hz
- `talkgroup` (integer): Talkgroup ID
- `source` (integer): Source radio ID
- `systemLabel` (string): Human-readable system name
- `talkgroupLabel` (string): Human-readable talkgroup name
- `talkgroupGroup` (string): Talkgroup category
- `talkerAlias` (string): Talker alias
- `patches` (string): Comma-separated list of patched talkgroups
- `test` (integer): Test mode flag (1 for test)

#### Response

**Success (200)**
```json
{
  "status": "ok",
  "message": "Call received and processed",
  "callId": "1_1234567890_100"
}
```

**Error (400/401/500)**
```json
{
  "detail": "Error message"
}
```

### Monitoring Endpoints

**GET** `/health`
```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00Z",
  "version": "1.0.0",
  "database": "connected"
}
```

**GET** `/metrics`
```json
{
  "total_calls": 1234,
  "calls_today": 56,
  "calls_last_hour": 7,
  "systems": {"1": 500, "2": 734},
  "talkgroups": {"100": 123, "200": 456},
  "upload_sources": {"192.168.1.100": 1234},
  "storage_used_mb": 567.8,
  "audio_files_count": 1234
}
```

## SDRTrunk Configuration

Configure SDRTrunk to use this API:

1. In SDRTrunk, go to Streaming configuration
2. Add a new RdioScanner stream
3. Set the following:
   - **URL**: `http://your-server:8080/api/call-upload`
   - **API Key**: Your configured API key
   - **System ID**: Your system ID (numeric)

## Database Schema

The SQLite database stores:

- **radio_calls**: Main call records with all metadata
- **upload_logs**: Audit trail of all upload attempts
- **system_stats**: Aggregated statistics by system
- **talkgroup_stats**: Aggregated statistics by talkgroup
- **api_keys**: API key management (if using database auth)

## Development

### Project Structure
```
rdioCallsAPI/
├── src/
│   ├── api/           # API endpoints
│   ├── database/      # Database management
│   ├── models/        # Data models
│   ├── services/      # Business logic
│   └── utils/         # Utilities
├── config.yaml        # Default configuration
├── main.py           # Entry point
└── requirements.txt  # Dependencies
```

### Extending the API

1. **Add new endpoints**: Create new routers in `src/api/`
2. **Add processing**: Implement in `src/services/`
3. **Add storage backends**: Extend `src/utils/file_handler.py`
4. **Add authentication**: Extend `src/api/rdioscanner.py`

### Production Deployment

For production, you can run directly with Hypercorn:

```bash
uv run hypercorn src.api:create_app --bind 0.0.0.0:8080 --workers 4
```

Or use the main.py script:

```bash
uv run python main.py --host 0.0.0.0 --port 8080
```

### Running Tests

```bash
uv run pytest tests/
```

## Troubleshooting

### Common Issues

1. **"Invalid API key"**: Check your API key configuration and ensure it matches what SDRTrunk is sending

2. **"File format not accepted"**: Ensure SDRTrunk is configured to send MP3 files

3. **Database locked errors**: Ensure WAL mode is enabled in configuration

4. **High memory usage**: Adjust `file_handling.max_file_size_mb` and implement cleanup schedules

### Debug Mode

Enable debug logging:
```yaml
logging:
  level: "DEBUG"
server:
  debug: true
```

## License

MIT License - see LICENSE file for details