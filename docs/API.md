# sdrtrunk-rdio-api - API Documentation

## Overview

sdrtrunk-rdio-api implements the RdioScanner protocol for receiving radio call uploads from SDRTrunk. The API is designed to be simple, secure, and modular.

## Authentication

API keys are configured in `config/config.yaml`. Each key must be 16-512
characters and have a unique, public `identifier`; restrictions are
optional:

- **IP-based restrictions**: Limit key usage to specific IP addresses
- **System-based restrictions**: Limit uploads and reads to specific system IDs
- **Required stable identifier**: Attribute audit records without deriving or
  recording any value from the secret

Uploads carry the key in RdioScanner's `key` multipart field. Every query,
audio, and metrics request carries it in `X-API-Key`:

```http
X-API-Key: your-generated-random-key
```

The Bash/Zsh examples below prompt into a non-exported shell variable. Their
built-in `printf` sends the credential to curl over standard input, so the key
does not appear in curl's process arguments. Clear the variable after use; for
repeatable uploads, prefer `scripts/test_upload.py --api-key-file` with a
mode-`0600` credential file.

```bash
RDIO_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key: "))')"
```

The server fails closed when no keys are configured. Anonymous access is
possible only through the explicit `allow_unauthenticated_uploads` and
`allow_unauthenticated_reads` compatibility switches; do not enable them on a
directly reachable service.

## Endpoints

### Upload Call

**POST** `/api/call-upload`

Upload a radio call recording with metadata.

#### Request

- **Method**: POST
- **Content-Type**: multipart/form-data

#### Form Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| key | string | Yes | API key for authentication |
| system | string | Yes | System ID (numeric string) |
| dateTime | integer | Yes | Unix timestamp in seconds |
| audio | file | Yes* | MP3 audio file (* not required for test mode) |
| frequency | integer | No | Frequency in Hz |
| talkgroup | integer | No | Talkgroup ID |
| source | integer | No | Source radio ID |
| systemLabel | string | No | Human-readable system name |
| talkgroupLabel | string | No | Human-readable talkgroup name |
| talkgroupGroup | string | No | Talkgroup category/group |
| talkerAlias | string | No | Alias of the talking radio |
| patches | string | No | Comma-separated list of patched talkgroups |
| frequencies | string | No | Comma-separated list of frequencies |
| sources | string | No | Comma-separated list of source IDs |
| talkgroupTag | string | No | Additional talkgroup tag |
| test | integer | No | Test mode flag (1 for test) |

`dateTime` may be at most five minutes ahead of server time. Text fields,
multipart part counts, and request bytes are bounded. Audio must have an
accepted extension/MIME type and a valid MP3 signature.

#### Response Formats

The API automatically detects the desired response format based on the `Accept` header:

- **JSON Response** (when Accept includes "application/json"):

```json
{
  "status": "ok",
  "message": "Call received and processed",
  "callId": "123"
}
```

`callId` is the database ID of the stored call - you can fetch the call
back with `GET /api/calls/123` or stream its audio with
`GET /api/calls/123/audio`.

- **Plain Text Response** (default):

```text
Call imported successfully.
```

#### Error Responses

- **401 Unauthorized**: Invalid or missing API key
- **403 Forbidden**: Valid key is not allowed from this IP or for this system
- **400 Bad Request**: Missing required fields or invalid data
- **415 Unsupported Media Type**: Request is not an accepted form encoding
- **413 Payload Too Large**: Audio file exceeds `max_file_size_mb`
- **507 Insufficient Storage**: Archive byte/file quota or filesystem byte/inode reserve reached
- **429 Too Many Requests**: Rate limit exceeded
- **500 Internal Server Error**: Server-side error

### Health Check

**GET** `/health`

Check if the API service is running and healthy.

This is a readiness response, not a pure process-liveness probe. It returns
HTTP 503 while the database is unavailable, persistent archive accounting is
uncertain/actively reconciling, or a worst-case upload cannot satisfy the
configured archive byte/file-count and filesystem byte/inode reserves. The
state-filesystem check also preserves the configured bounded SQLite/WAL
maintenance reserve. Retention and recovery remain available while archive
ingestion is fail-closed.

#### Response

```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00Z",
  "version": "1.0.0",
  "database": "connected"
}
```

### Query Calls

**GET** `/api/calls`

Requires `X-API-Key`. Supports `system_id`, `talkgroup_id`, `source_id`,
`frequency`, `date_from`, `date_to`, `hours_ago`, pagination, and the documented
sort fields. Results and totals include only systems allowed for the key.

```bash
printf 'X-API-Key: %s\n' "$RDIO_API_KEY" | \
  curl --header @- \
    "http://localhost:8080/api/calls?system_id=1&per_page=20"
```

**GET** `/api/calls/{call_id}` returns one in-scope call. **GET** `/api/systems`
and **GET** `/api/talkgroups` return scoped summaries. All require the same
header; inaccessible call IDs return 404 rather than revealing their existence.

### Metrics

**GET** `/metrics`

Get statistics about the API usage and stored data.

Requires `X-API-Key`. Results include only systems allowed for that key.

#### Response

```json
{
  "total_calls": 1234,
  "calls_today": 56,
  "calls_last_hour": 7,
  "systems": {
    "1": 500,
    "2": 734
  },
  "talkgroups": {
    "100 (Police Dispatch)": 123,
    "200 (Fire/EMS)": 456
  },
  "upload_sources": {
    "192.168.1.100": 1234
  },
  "storage_used_mb": 567.8,
  "audio_files_count": 1234
}
```

### Get Call Audio

**GET** `/api/calls/{call_id}/audio`

Stream the audio file for a specific radio call.

Requires `X-API-Key`; an out-of-scope call is returned as not found.

#### Parameters

| Parameter | Type | Location | Description |
|-----------|------|----------|-------------|
| call_id | integer | path | The database ID of the call |

#### Response

- **200 OK**: Returns the audio file with `Content-Type: audio/mpeg`
- **404 Not Found**: Call does not exist or has no audio file

#### Example

```bash
printf 'X-API-Key: %s\n' "$RDIO_API_KEY" | \
  curl --header @- --output call_123.mp3 \
    http://localhost:8080/api/calls/123/audio
```

## Test Mode

The API supports a test mode for verifying connectivity without storing data. To use test mode, include `test=1` in the form data. The API will respond without processing or storing the upload.

Test requests are authenticated: a test with an invalid API key returns
401, so SDRTrunk's "Test" button genuinely verifies your key.

## Rate Limiting

Limits come from `security.rate_limit` in config.yaml. Defaults:

- 600 requests per minute
- 10,000 requests per hour
- 100,000 requests per day

These limits are applied per server-resolved client IP and can be configured in
`config.yaml`. An arbitrary `X-API-Key` header never selects a new rate-limit
bucket. `X-Forwarded-For` is used only through explicitly trusted proxy hops.

## Examples

### cURL Upload Example

```bash
printf '%s' "$RDIO_API_KEY" | \
  curl http://localhost:8080/api/call-upload \
    --form 'key=<-' \
    --form 'system=1' \
    --form 'dateTime=1704123456' \
    --form 'frequency=460000000' \
    --form 'talkgroup=100' \
    --form 'systemLabel=My System' \
    --form 'talkgroupLabel=Dispatch' \
    --form 'audio=@recording.mp3'
```

### Python Upload Example

```python
import getpass
import requests
import time

url = "http://localhost:8080/api/call-upload"
data = {
    'key': getpass.getpass('API key: '),
    'system': '1',
    'dateTime': str(int(time.time())),
    'frequency': '460000000',
    'talkgroup': '100',
    'systemLabel': 'My System',
    'talkgroupLabel': 'Dispatch'
}

with open('recording.mp3', 'rb') as audio:
    files = {'audio': ('recording.mp3', audio, 'audio/mpeg')}
    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=(5, 30),
        allow_redirects=False,
    )

data['key'] = ''
print(response.json())
```

### Test Mode Example

```bash
printf '%s' "$RDIO_API_KEY" | \
  curl http://localhost:8080/api/call-upload \
    --form 'key=<-' \
    --form 'system=1' \
    --form 'test=1'
```

```bash
unset RDIO_API_KEY
```

## SDRTrunk Integration

### Quick Setup

To configure SDRTrunk to use this API:

1. **Open SDRTrunk**
2. **Go to Playlist Editor** (View menu → Playlist Editor)
3. **Click the "Streaming" tab** (at the bottom of the window)
4. **Click the "+" button** to add a new stream
5. **Select "RdioScanner"** from the dropdown menu
6. **Fill in these settings:**
   - **Host**: `localhost` (if on same computer) or your server's IP address
   - **Port**: `8080` (or whatever you configured)
   - **API Key**: The key you set in your config.yaml file
   - **System ID**: A number representing your radio system (like `1`)
7. **Click "Test"** to verify the connection works
8. **Click "Save"** to keep the settings

### Detailed Configuration

**Host Field:**

- Use `localhost` if SDRTrunk and the server are on the same computer
- Use your server's IP address (like `192.168.1.100`) if they're on different computers
- Use your domain name if you have one set up

**Port Field:**

- Default is `8080`
- Must match the `port` setting in your server's config.yaml
- Common alternatives: `8000`, `9000`, `3000`

**API Key Field:**

- Must exactly match one of the `api_keys` in your config.yaml
- Case-sensitive
- No extra spaces or quotes

**System ID Field:**

- A number that identifies your radio system
- Can be any number (1, 123, 456, etc.)
- Used to organize recordings from different systems
- If you're monitoring multiple systems, use different numbers for each

### Testing the Connection

After configuring, always test the connection:

1. Click the **"Test" button** in SDRTrunk
2. You should see **"Test successful!"**
3. If it fails, check:
   - Server is running
   - Host and port are correct
   - API key matches exactly
   - No firewall blocking the connection

### Multiple SDRTrunk Instances

You can connect multiple SDRTrunk instances to the same server:

- Use the same or different API keys
- Use different System IDs for each instance
- Each will appear separately in the statistics

## Security Considerations

1. **Always use API keys** in production environments
2. **Use HTTPS** when deploying on public networks
3. **Configure IP restrictions** for additional security
4. **Monitor upload logs** for suspicious activity
5. **Set appropriate rate limits** based on your needs
6. **Implement file retention policies** to manage storage
