# RdioCallsAPI

[![CI](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/ci.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/ci.yml)
[![Advanced CI/CD](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/advanced-ci.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/advanced-ci.yml)
[![CI/CD Pipeline](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/swiftraccoon/sdrtrunk-rdio-api/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/swiftraccoon/sdrtrunk-rdio-api/branch/main/graph/badge.svg)](https://codecov.io/gh/swiftraccoon/sdrtrunk-rdio-api)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
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

- **Python 3.13** installed on your computer
- **SDRTrunk** set up and working with your radio system
- **Basic command line knowledge** (we'll guide you through it)

### Installing Python 3.13

**Windows:**

1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Download Python 3.13
3. Run the installer and check "Add Python to PATH"

**Mac:**

```bash
# Using Homebrew (install Homebrew first if you don't have it)
brew install python@3.13
```

**Linux (Ubuntu/Debian):**

```bash
sudo apt update
sudo apt install python3.13 python3.13-venv
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
# Copy the example config
cp config.example.yaml config.yaml
```

### Step 4: Set Your API Key

Open `config.yaml` in any text editor (Notepad, TextEdit, etc.) and change this line:

```yaml
security:
  api_keys:
    - key: "change-me-to-something-secret"  # ⚠️ CHANGE THIS!
      description: "My SDRTrunk"
```

Pick any password you want - just remember it for SDRTrunk!

### Step 5: Start the Server

```bash
uv run python cli.py serve
```

You should see:

```
🚀 Starting RdioCallsAPI Server
├─ Address: http://0.0.0.0:8080
└─ API Keys: 1 configured

Press Ctrl+C to stop the server
```

### Step 6: Configure SDRTrunk

1. Open SDRTrunk → Playlist Editor → Streaming tab
2. Add new stream:
   - **Type:** `RdioScanner`
   - **Host:** `localhost` (or your computer's IP address)
   - **Port:** `8080`
   - **API Key:** *(the password you set in config.yaml)*
   - **System ID:** `1`
3. Click "Test" - you should see "Test successful!"
4. Save and start your playlist

## Verifying It's Working

Open your web browser and go to: `http://localhost:8080/health`

You should see "healthy" - that means it's working!

Your recordings will be saved in the `data/audio/` folder.

## Useful Commands

```bash
# Start the server
uv run python cli.py serve

# See recent calls
uv run python cli.py stats

# Clean up old files (30+ days)
uv run python cli.py clean --days 30

# Get help
uv run python cli.py --help
```

## Configuration Options

### Storage Settings

```yaml
file_handling:
  storage:
    strategy: "filesystem"      # Where to store files
    directory: "data/audio"     # Storage folder
    organize_by_date: true      # Organize into date folders
    retention_days: 30          # Delete files older than this (0 = keep forever)
```

### Processing Modes

```yaml
processing:
  mode: "store"  # What to do with calls
```

- **`log_only`**: Just keep the information, don't save audio files
- **`store`**: Save audio files and information (recommended)
- **`process`**: Save everything and allow future processing

### Security Options

```yaml
security:
  api_keys:
    - key: "your-secret-key"
      description: "SDRTrunk in basement"
      allowed_ips: ["192.168.1.100"]    # Optional: only allow from this IP
      allowed_systems: ["1", "2"]       # Optional: only allow these system IDs
  
  rate_limit:
    enabled: true
    max_requests_per_minute: 60        # Prevent spam
```

## Troubleshooting

### "Connection refused" or "Can't connect"

1. **Check if server is running**: Look for the startup message
2. **Check the port**: Make sure SDRTrunk uses the same port as your config
3. **Check firewalls**: Make sure port 8080 (or your port) is open
4. **Check the IP address**: Use your computer's actual IP address, not `localhost` if SDRTrunk is on a different computer

### "Invalid API key"

1. **Check your config.yaml**: Make sure the API key is correct
2. **Check SDRTrunk**: Make sure the API key matches exactly
3. **Restart the server** after changing config.yaml

### "File format not supported"

1. **Check SDRTrunk audio settings**: Make sure it's set to MP3
2. **Check file size limits** in your config.yaml

### Server won't start

1. **Check Python version**: Run `python3 --version` (should be 3.13+)
2. **Check dependencies**: Run `uv sync` again
3. **Check config file**: Run `uv run python cli.py init` to regenerate
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

3. **View recent activity**: `uv run python cli.py stats`

4. **Test the connection**: `uv run python cli.py test-db`

## Advanced Usage

### Running as a Service (Linux)

Create `/etc/systemd/system/rdiocalls.service`:

```ini
[Unit]
Description=RdioCallsAPI Server
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/rdioCallsAPI
ExecStart=/home/your-username/.cargo/bin/uv run python cli.py serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rdiocalls
sudo systemctl start rdiocalls
```

### Running Behind a Reverse Proxy

If you're using nginx or Apache, the server works great behind a proxy. Just make sure to:

1. Forward the correct headers
2. Set appropriate timeouts for file uploads
3. Configure SSL/TLS termination at the proxy level

### Multiple SDRTrunk Instances

You can have multiple SDRTrunk instances connect to the same server:

```yaml
security:
  api_keys:
    - key: "scanner1-key"
      description: "Living room scanner"
      allowed_systems: ["1"]
    - key: "scanner2-key"
      description: "Garage scanner"
      allowed_systems: ["2", "3"]
```

## What Gets Stored

### Audio Files

- **Location**: `data/audio/YYYY/MM/DD/SYSTEM/`
- **Format**: MP3 files from SDRTrunk
- **Naming**: `YYYYMMDD_HHMMSS_TG{talkgroup}.mp3`

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
- **Health**: `GET /health` (check if server is working)
- **Stats**: `GET /metrics` (see activity statistics)
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
   - Your config.yaml (remove any API keys!)
