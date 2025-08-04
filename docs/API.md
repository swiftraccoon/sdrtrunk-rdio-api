# RdioCallsAPI - API Documentation

## Overview

RdioCallsAPI implements the RdioScanner protocol for receiving radio call uploads from SDRTrunk. The API is designed to be simple, secure, and modular.

## Authentication

API keys can be configured in the `config.yaml` file. Each key can have optional restrictions:

- **IP-based restrictions**: Limit key usage to specific IP addresses
- **System-based restrictions**: Limit key usage to specific system IDs

If no API keys are configured, the API operates in open mode (not recommended for production).

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

#### Response Formats

The API automatically detects the desired response format based on the `Accept` header:

- **JSON Response** (when Accept includes "application/json"):
```json
{
  "status": "ok",
  "message": "Call received and processed",
  "callId": "1_1704123456_100"
}
```

- **Plain Text Response** (default):
```
Call imported successfully.
```

#### Error Responses

- **401 Unauthorized**: Invalid or missing API key
- **400 Bad Request**: Missing required fields or invalid data
- **500 Internal Server Error**: Server-side error

### Health Check

**GET** `/health`

Check if the API service is running and healthy.

#### Response

```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00Z",
  "version": "1.0.0",
  "database": "connected"
}
```

### Metrics

**GET** `/metrics`

Get statistics about the API usage and stored data.

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

## Test Mode

The API supports a test mode for verifying connectivity without storing data. To use test mode, include `test=1` in the form data. The API will respond without processing or storing the upload.

## Rate Limiting

When rate limiting is enabled, the following limits apply by default:

- 60 requests per minute
- 1,000 requests per hour
- 10,000 requests per day

These limits are applied per IP address and can be configured in `config.yaml`.

## Examples

### cURL Upload Example

```bash
curl -X POST http://localhost:8080/api/call-upload \
  -F "key=your-api-key" \
  -F "system=1" \
  -F "dateTime=1704123456" \
  -F "frequency=460000000" \
  -F "talkgroup=100" \
  -F "systemLabel=My System" \
  -F "talkgroupLabel=Dispatch" \
  -F "audio=@recording.mp3"
```

### Python Upload Example

```python
import requests
import time

url = "http://localhost:8080/api/call-upload"
data = {
    'key': 'your-api-key',
    'system': '1',
    'dateTime': str(int(time.time())),
    'frequency': '460000000',
    'talkgroup': '100',
    'systemLabel': 'My System',
    'talkgroupLabel': 'Dispatch'
}
files = {
    'audio': ('recording.mp3', open('recording.mp3', 'rb'), 'audio/mpeg')
}

response = requests.post(url, data=data, files=files)
print(response.json())
```

### Test Mode Example

```bash
curl -X POST http://localhost:8080/api/call-upload \
  -F "key=your-api-key" \
  -F "system=1" \
  -F "test=1"
```

## SDRTrunk Integration

To configure SDRTrunk to use this API:

1. Open SDRTrunk
2. Go to **Playlist Editor** → **Streaming**
3. Add a new **RdioScanner** configuration
4. Configure as follows:
   - **Host**: Your server hostname/IP
   - **Port**: Your server port (default 8080)
   - **API Key**: Your configured API key
   - **System ID**: Your numeric system ID
   - **Test**: Click to verify connectivity

## Security Considerations

1. **Always use API keys** in production environments
2. **Use HTTPS** when deploying on public networks
3. **Configure IP restrictions** for additional security
4. **Monitor upload logs** for suspicious activity
5. **Set appropriate rate limits** based on your needs
6. **Implement file retention policies** to manage storage