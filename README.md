# sdrtrunk-rdio-api

[![ubuntu-latest](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-linux.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-linux.yml)
[![windows-latest](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-windows.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-windows.yml)
[![macos-latest](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-macos.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/test-macos.yml)
[![Security](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/security.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/security.yml)
[![Lint](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/lint.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/lint.yml)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![codecov](https://codecov.io/gh/swiftraccoon/sdrtrunk-rdio-api/branch/main/graph/badge.svg)](https://codecov.io/gh/swiftraccoon/sdrtrunk-rdio-api)

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A simple, easy-to-use API server that receives radio call recordings from SDRTrunk and stores them for later use.

## What Does This Do?

If you're using SDRTrunk to record radio communications, this server will:

- Receive audio files from SDRTrunk automatically
- Store them organized by date and system
- Keep track of all the details (frequency, talkgroup, etc.)
- Provide a web interface to see statistics
- Work with any scanner or radio system SDRTrunk supports

## Requirements

Before you start, you need:

- **Python 3.11+** installed on your computer
- **SDRTrunk** set up and working with your radio system
- **Basic command line knowledge** (we'll guide you through it)

### Installing Python 3.11+

**Windows:**

1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Download Python 3.11 or newer
3. Run the installer and check "Add Python to PATH"

**Mac:**

```bash
# Using Homebrew (install Homebrew first if you don't have it)
brew install python@3.11
```

**Linux (Ubuntu/Debian):**

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv
```

## Quick Setup (5 minutes)

> 💡 **First time?** Just copy and paste these commands into Terminal (Mac/Linux) or Command Prompt (Windows).

### Step 1: Get the Code

Open your terminal/command prompt and run:

```bash
# Download the project
git clone https://github.com/swiftraccoon/sdrtrunk-rdio-api.git
cd sdrtrunk-rdio-api
```

### Step 2: Install Dependencies

We use a tool called `uv` to manage dependencies. Install it:

**Windows/Mac/Linux:**

```bash
# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (alternative using PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install the project dependencies:

```bash
uv sync
```

### Step 3: Create Your Configuration

```bash
# Generate config/config.yaml
uv run sdrtrunk-rdio-api init
```

(Equivalent on POSIX systems:
`install -m 600 config/config.example.yaml config/config.yaml`.)

### Step 4: Generate and Set Your API Key

Generate a random key (the command prints it once):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Open `config/config.yaml` in any text editor (Notepad, TextEdit, etc.),
find the `security:` section, un-comment the key lines, and paste the generated
value:

```yaml
security:
  api_keys:
    - key: "PASTE-THE-GENERATED-RANDOM-KEY-HERE"
      identifier: "main-scanner"
      description: "My SDRTrunk"
      allowed_ips: []
      allowed_systems: []
```

Keys must be 16-512 characters and each entry needs a unique, non-secret
`identifier`. A missing, blank, too-short, or misspelled configuration fails
startup; the service does not silently become public. Existing configurations
without identifiers must add them before upgrading.
Store the key in a password manager and enter the same value in SDRTrunk.

### Step 5: Start the Server

```bash
uv run sdrtrunk-rdio-api serve
```

You should see:

```text
>> Starting sdrtrunk-rdio-api Server
  - Config: config/config.yaml
  - Address: http://127.0.0.1:8080
  ...
  - API Keys: 1 configured

Press Ctrl+C to stop the server
```

### Step 6: Configure SDRTrunk

1. Open SDRTrunk → Playlist Editor → Streaming tab
2. Add new stream:
   - **Type:** `RdioScanner`
   - **Host:** `localhost` (or your computer's IP address)
   - **Port:** `8080`
   - **API Key:** *(the password you set in config/config.yaml)*
   - **System ID:** `1`
3. Click "Test" - you should see "Test successful!"
4. Save and start your playlist

The default bind address is localhost-only. If SDRTrunk runs on another
machine, set `server.host: "0.0.0.0"` only after restricting port 8080 with a
host firewall to the scanner/proxy addresses (and use TLS for untrusted
networks).

## Verifying It's Working

Open your web browser and go to: `http://localhost:8080/health`

You should see "healthy" - that means it's working!

Your recordings will be saved in the `data/audio/` folder.

## Useful Commands

```bash
# Start the server
uv run sdrtrunk-rdio-api serve

# See recent calls
uv run sdrtrunk-rdio-api stats

# Clean up old files (30+ days)
uv run sdrtrunk-rdio-api clean --days 30

# Get help
uv run sdrtrunk-rdio-api --help
```

The server, destructive `clean`, and long-snapshot `export` commands take an
exclusive process lock per database directory. Stop the server before running
`clean` or `export`; the CLI refuses to proceed if another protected process is active.
This prevents offline cleanup from racing a live staged upload and prevents a
large export transaction from pinning the live server's WAL.
`stats`, `test-db`, and the pre-confirmation cleanup preview instead use
SQLite's URI read-only mode plus `query_only`; they cannot create/repair schema
or change the running service's journal mode, and close each bounded snapshot
before writing terminal output.
CLI commands use console-only logging. The server exclusively locks its rotating
log family so concurrent processes cannot race a rollover.

## Configuration Options

### Storage Settings

```yaml
file_handling:
  max_file_size_mb: 100         # Per-upload parsing/storage bound
  minimum_free_space_mb: 256    # Reserve on upload, DB, and log filesystems
  minimum_free_inodes: 1024     # Preserve filesystem metadata headroom
  maintenance_state_reserve_mb: 32 # Preserve bounded SQLite/WAL headroom
  storage:
    strategy: "filesystem"      # Where to store files
    directory: "data/audio"     # Storage folder
    max_storage_size_mb: 102400 # Total persistent audio-archive quota
    max_storage_files: 5000000  # Independent persistent file-count quota
    organize_by_date: true      # Organize into date folders (UTC dates)
    retention_days: 30          # Delete calls older than this (0 = keep forever)
    cleanup_interval_hours: 6   # How often the server enforces retention
```

Retention is enforced automatically by the running server: calls older than
`retention_days` are removed from the database together with their audio
files and upload logs. You can also clean manually with
`sdrtrunk-rdio-api clean`.

Upload admission is conservative. Before reading a body, the server reserves
up to `max_file_size_mb` for multipart spooling plus a small SQLite/log write
margin. After authentication and metadata validation, filesystem storage also
reserves the application-temp and destination stages and claims one maximum-size
archive byte/file slot. The server also reserves the worst-case file and
directory inodes needed by each spool, state-write, temp, and destination stage;
reservations shrink to actual archive size/count as stages complete. An
additional `maintenance_state_reserve_mb` remains protected for each distinct
database/log filesystem; bounded retention phases claim it atomically and
checkpoint before releasing it. Requests
that would cross `max_storage_size_mb`/`max_storage_files`, leave less than
`minimum_free_space_mb`, or consume `minimum_free_inodes` headroom receive HTTP
507. Filesystems that explicitly report no fixed inode pool are bounded by the
persistent file-count quota instead.

Capacity accounting is coordinated within one server process. The supported
CLI runs one Hypercorn worker. If the app is embedded behind multiple ASGI
worker processes, enforce a shared database/external quota as well. Filesystem
free-space checks use `statvfs` and are necessarily best-effort against writes
from other processes. Periodic reconciliation streams the complete archive in
bounded background slices without following symlinks; very large archives keep
upload admission closed until all slices complete.

Archive query work is also bounded by elapsed time and SQLite virtual-machine
steps. Aggregate and sort scratch can still consume temporary disk before that
bound fires. Docker Compose directs SQLite scratch to its 64 MiB `/tmp` tmpfs;
for a native deployment, set `SQLITE_TMPDIR` to a private, quota-limited
filesystem sized for your expected queries. Before creating a missing required
index, startup checks the actual SQLite scratch filesystem (`SQLITE_TMPDIR`
before `TMPDIR` on Unix). A separate scratch filesystem must have roughly
`max(main database + WAL size, 32 MiB) + 32 MiB` free; if it shares the
database device, startup also preserves the configured persistent-state
reserve. Temporarily increase the Compose `/tmp` size for a larger legacy
database upgrade.
`/health` is a readiness check: it returns HTTP 503 while persistent archive
accounting is uncertain/reconciling, or a worst-case new upload cannot satisfy
the byte, file-count, free-space, and free-inode limits. The process itself
remains live and retention/recovery work continues.

New database rows store audio locations as storage-root-relative POSIX
references, so moving the complete archive and changing `storage.directory`
keeps new references valid. Legacy absolute-path rows are intentionally not
rewritten automatically: migrate them explicitly while the old configured root
is known, before changing that root.

### Processing Modes

```yaml
processing:
  mode: "store"  # What to do with calls
```

- **`log_only`**: Just keep the information, don't save audio files
- **`store`**: Save audio files and information (recommended)
- **`process`**: Currently behaves like `store` (reserved for future use)

### Security Options

```yaml
security:
  api_keys:
    - key: "PASTE-A-LONG-RANDOM-KEY-HERE"
      identifier: "basement-scanner"
      description: "SDRTrunk in basement"
      allowed_ips: ["192.168.1.100"]    # Optional: only allow from this IP
      allowed_systems: ["1", "2"]       # Optional: only allow these system IDs

  # Only needed behind a reverse proxy: proxy IPs whose
  # X-Forwarded-For header should be trusted for allowed_ips checks
  trusted_proxies: []

  # Dangerous compatibility switches. Leave both false unless a trusted
  # upstream layer provides equivalent authentication.
  allow_unauthenticated_uploads: false
  allow_unauthenticated_reads: false

  rate_limit:
    enabled: true
    max_requests_per_minute: 600       # Sized for busy trunked systems
    max_requests_per_hour: 10000
    max_requests_per_day: 100000
```

API keys protect uploads, query endpoints, metrics, and stored audio. Supply
the key in SDRTrunk's form field for uploads and in the `X-API-Key` header for
read requests. `allowed_systems` scopes both uploads and reads. `/health`
remains public and exposes only service/database status.

```bash
RDIO_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key: "))')"
printf 'X-API-Key: %s\n' "$RDIO_API_KEY" | \
  curl --header @- http://localhost:8080/metrics
unset RDIO_API_KEY
```

## Troubleshooting

### "Connection refused" or "Can't connect"

1. **Check if server is running**: Look for the startup message
2. **Check the port**: Make sure SDRTrunk uses the same port as your config
3. **Check firewalls**: Make sure port 8080 (or your port) is open
4. **Check the IP address**: Use your computer's actual IP address, not `localhost` if SDRTrunk is on a different computer

### "Invalid API key"

1. **Check your config/config.yaml**: Make sure the API key is correct
2. **Check SDRTrunk**: Make sure the API key matches exactly
3. **Restart the server** after changing config/config.yaml

### "File format not supported"

1. **Check SDRTrunk audio settings**: Make sure it's set to MP3
2. **Check file size limits** in your config/config.yaml

### Server won't start

1. **Check Python version**: Run `python3 --version` (should be 3.11+)
2. **Check dependencies**: Run `uv sync` again
3. **Check config file**: Run `uv run sdrtrunk-rdio-api init` to regenerate
4. **Check logs**: Look for error messages in the console

### Getting More Help

1. **Enable debug logging**:

   ```yaml
   logging:
     level: "DEBUG"
   server:
     debug: true
   ```

2. **Check the health endpoint**: `http://localhost:8080/health`

3. **View recent activity**: `uv run sdrtrunk-rdio-api stats`

4. **Test the connection**: `uv run sdrtrunk-rdio-api test-db`

## Advanced Usage

### Running as a Service (Linux)

Create `/etc/systemd/system/rdiocalls.service`:

```ini
[Unit]
Description=sdrtrunk-rdio-api Server
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/rdioCallsAPI
# uv installs to ~/.local/bin by default; check with `which uv`
ExecStart=/home/your-username/.local/bin/uv run sdrtrunk-rdio-api serve
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rdiocalls
sudo systemctl start rdiocalls
```

### Docker Compose

The image runs as non-root UID 1000 and refuses a configuration file that is
readable by its group or by other users. On a Linux Docker host, prepare the
bind-mounted file before starting Compose:

```bash
chmod 600 config/config.yaml
sudo chown 1000:1000 config/config.yaml
docker compose up --build
```

If user-namespace remapping is enabled, use the UID mapped to container UID
1000. Verify what the container sees with
`docker compose run --rm --no-deps --entrypoint stat sdrtrunk-rdio-api -c
'%u %a' /app/config/config.yaml`; the result must be `1000 600` in a standard
rootful deployment.

Docker Desktop, rootless Docker, and user-namespace remapping can present a
host bind mount with different ownership or modes inside the container. In
particular, host-native Windows mounts can appear as mode `0777`, while macOS
bind mounts retain host permissions and may not be readable by container UID
1000 after being restricted to mode `0600`. Do not weaken the application's
mode check or make the file `0644`. On Windows, keep the project on a WSL
filesystem that preserves POSIX modes. Otherwise, replace the bind mount with
a deployment-managed config/secret volume provisioned for UID 1000 with mode
`0600`.

### Running Behind a Reverse Proxy

If you're using nginx or Apache, the server works great behind a proxy. Just make sure to:

1. Forward the correct headers (`X-Forwarded-For` for client IPs)
2. Add your proxy's IP to `security.trusted_proxies` in config.yaml,
   otherwise `allowed_ips` restrictions will see the proxy's address
3. Set appropriate request-body timeouts for file uploads and response-idle
   timeouts for audio downloads. The application enforces a 15-minute absolute
   audio-response lifetime and applies `server.read_timeout_seconds` as an
   absolute deadline for each HTTP/2 request body, but Hypercorn does not
   provide a response-write idle timeout; the proxy must terminate stalled
   downstream connections.
4. Configure SSL/TLS at the proxy, or set both `server.ssl_cert` and
   `server.ssl_key` to use the built-in TLS support

Keep the application bound to `127.0.0.1` when the proxy is on the same host.
Never add an address to `trusted_proxies` unless that peer overwrites/appends
`X-Forwarded-For` correctly and clients cannot connect through it unchecked.

### Multiple SDRTrunk Instances

You can have multiple SDRTrunk instances connect to the same server:

```yaml
security:
  api_keys:
    - key: "PASTE-A-UNIQUE-LONG-RANDOM-KEY-FOR-SCANNER-1"
      identifier: "living-room"
      description: "Living room scanner"
      allowed_systems: ["1"]
    - key: "PASTE-A-UNIQUE-LONG-RANDOM-KEY-FOR-SCANNER-2"
      identifier: "garage"
      description: "Garage scanner"
      allowed_systems: ["2", "3"]
```

## What Gets Stored

### Audio Files

- **Location**: `data/audio/YYYY/MM/DD/SYSTEM/` (UTC dates)
- **Format**: MP3 files from SDRTrunk
- **Naming**: `YYYYMMDD_HHMMSS_SYS{system}_TG{talkgroup}_{label}...mp3`
  (includes frequency and source radio when available)

### Database Information

- Call timestamp and duration
- System and talkgroup information
- Frequency and source radio
- Audio file location and size
- Upload source and API key used

### Log Files

- **Location**: `logs/rdio_calls_api.log`
- **Contains**: All server activity and errors
- **Rotation**: Automatic when files get too large

## API Reference

See the full API documentation at: [docs/API.md](docs/API.md)

Quick reference:

- **Upload**: `POST /api/call-upload` (used by SDRTrunk)
- **Query**: `GET /api/calls` (search stored calls; `X-API-Key` required)
- **Audio**: `GET /api/calls/{id}/audio` (stream audio; `X-API-Key` required)
- **Health**: `GET /health` (check if server is working)
- **Stats**: `GET /metrics` (statistics; `X-API-Key` required)
- **Docs**: `GET /docs` (interactive API documentation)

## Support

If you need help:

1. Check this README first
2. Look at the troubleshooting section
3. Check the [API documentation](docs/API.md)
4. Check our [Getting Started Guide](docs/GETTING_STARTED.md) for common issues
5. Open an issue on GitHub with:
   - Your operating system
   - Python version (`python3 --version`)
   - Error messages
   - Your config/config.yaml (remove any API keys!)
