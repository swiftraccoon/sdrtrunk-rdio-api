# Getting Started with sdrtrunk-rdio-api

This guide will walk you through setting up sdrtrunk-rdio-api step-by-step, even if you're new to running servers or working with command lines.

## What You Need

Before starting, make sure you have:

- A computer running Windows, Mac, or Linux
- SDRTrunk already working with your radio setup
- About 15 minutes to set everything up

## Installation Guide

### Step 1: Install Python

sdrtrunk-rdio-api needs Python 3.11+ to run.

**On Windows:**

1. Go to <https://www.python.org/downloads/>
2. Click "Download Python 3.11+" (3.11 or newer)
3. Run the installer
4. **IMPORTANT**: Check the box "Add Python to PATH"
5. Click "Install Now"

**On Mac:**
If you have Homebrew installed:

```bash
brew install python@3.11
```

If you don't have Homebrew, download from python.org like Windows.

**On Linux (Ubuntu/Debian):**

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv git
```

### Step 2: Download sdrtrunk-rdio-api

Open Terminal (Mac/Linux) or Command Prompt (Windows) and run:

```bash
# Download the code
git clone https://github.com/swiftraccoon/sdrtrunk-rdio-api.git
cd sdrtrunk-rdio-api
```

### Step 3: Install the uv Package Manager

uv makes it easy to manage Python dependencies:

```bash
# On Windows, Mac, or Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**On Windows (if the above doesn't work):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close your terminal and open a new one, then run:

```bash
uv --version
```

You should see a version number like `uv 0.x.x`.

### Step 4: Install Dependencies

```bash
# Install all the Python packages needed
uv sync
```

This might take a minute or two the first time.

### Step 5: Create Your Configuration

```bash
# Generate a sample config file
uv run sdrtrunk-rdio-api init

# Copy it to become your main config
cp config.example.yaml config.yaml
```

### Step 6: Edit Your Configuration

Open `config.yaml` in any text editor (Notepad, TextEdit, etc.) and change:

```yaml
# Find the security section and change this:
security:
  api_keys:
    - key: "your-secret-api-key-here"  # Change this to something unique
      description: "My SDRTrunk Setup"
```

**Important**: Make your API key something only you know, like `my-scanner-2025-secret`.

### Step 7: Test the Server

```bash
uv run sdrtrunk-rdio-api serve
```

You should see:

```
🚀 Starting sdrtrunk-rdio-api Server
├─ Address: http://0.0.0.0:8080
├─ HTTP/2: Enabled (required for SDRTrunk)
├─ Processing Mode: store
├─ Debug Mode: False
├─ API Docs: http://0.0.0.0:8080/docs
├─ Database: data/rdio_calls.db
├─ Audio Storage: data/audio
└─ API Keys: 1 configured

Press Ctrl+C to stop the server
```

Great! Your server is running. Keep this window open and open a new terminal/command prompt for the next steps.

### Step 8: Test the Web Interface

Open your web browser and go to:

- <http://localhost:8080/health>

You should see something like:

```json
{
  "status": "healthy",
  "timestamp": "2025-01-15T10:30:00Z",
  "version": "1.0.0",
  "database": "connected"
}
```

### Step 9: Configure SDRTrunk

Now we need to tell SDRTrunk to send audio files to your server:

1. **Open SDRTrunk**
2. **Open Playlist Editor** (from the View menu)
3. **Click the "Streaming" tab** (at the bottom)
4. **Click the "+" button** to add a new stream
5. **Select "RdioScanner"** from the dropdown
6. **Fill in the settings:**
   - **Host**: `localhost` (if SDRTrunk is on the same computer)
   - **Port**: `8080`
   - **API Key**: `your-secret-api-key-here` (whatever you put in config.yaml)
   - **System ID**: `1` (or whatever number represents your radio system)
7. **Click "Test"** - you should see "Test successful!"
8. **Click "Save"**

### Step 10: Start Recording

1. **In SDRTrunk, start your playlist** (the play button)
2. **Make sure your streaming is enabled** (there should be a green indicator)
3. **Wait for some radio traffic**

### Step 11: Verify It's Working

In your second terminal window, run:

```bash
uv run sdrtrunk-rdio-api stats --last 5
```

If everything is working, you'll see recent calls listed. If not, see the troubleshooting section below.

## What Happens Now?

When SDRTrunk receives radio transmissions:

1. SDRTrunk records the audio as an MP3 file
2. SDRTrunk sends the MP3 and information to your sdrtrunk-rdio-api server
3. Your server saves the MP3 file in `data/audio/`
4. Your server records all the details in a database
5. You can view statistics and information through the web interface

## File Organization

Your audio files will be organized like this:

```
data/audio/
├── 2025/
│   ├── 01/
│   │   ├── 15/
│   │   │   ├── 1/           # System ID 1
│   │   │   │   ├── 20250115_143022_TG100.mp3
│   │   │   │   ├── 20250115_143045_TG200.mp3
│   │   │   │   └── ...
```

## Useful Commands

```bash
# View recent activity
uv run sdrtrunk-rdio-api stats

# View recent activity with more details
uv run sdrtrunk-rdio-api stats --last 20

# Check if everything is working
uv run sdrtrunk-rdio-api test-db

# Start the server
uv run sdrtrunk-rdio-api serve

# Start with debug logging (if you have problems)
uv run sdrtrunk-rdio-api serve --log-level DEBUG
```

## Web Interface

Once your server is running, you can view:

- **Server health**: <http://localhost:8080/health>
- **Usage statistics**: <http://localhost:8080/metrics>
- **API documentation**: <http://localhost:8080/docs>

## Stopping the Server

To stop the server, go back to the terminal where it's running and press `Ctrl+C`.

## Troubleshooting

### "Command not found" errors

- Make sure Python is installed and in your PATH
- Make sure uv is installed (`uv --version` should work)
- Try closing and reopening your terminal

### "Connection refused" when testing SDRTrunk

- Make sure the server is running (you should see the startup message)
- Check that the port number matches (8080 by default)
- If SDRTrunk is on a different computer, use that computer's IP address instead of "localhost"

### "Invalid API key" errors

- Make sure the API key in SDRTrunk exactly matches what's in your config.yaml
- Make sure there are no extra spaces or quotes
- Restart the server after changing config.yaml

### No calls showing up

- Check that SDRTrunk is actually receiving radio traffic
- Make sure the streaming is enabled in SDRTrunk (green indicator)
- Check the server logs for error messages
- Try running with debug logging: `uv run sdrtrunk-rdio-api serve --log-level DEBUG`

### Server won't start

- Check that no other program is using port 8080
- Try a different port: `uv run sdrtrunk-rdio-api serve --port 8081`
- Check that your config.yaml file is valid YAML (proper indentation)

## Next Steps

Once everything is working:

1. Consider setting up the server to start automatically (see the main README)
2. Set up file cleanup to manage disk space (`uv run sdrtrunk-rdio-api clean --help`)
3. Explore the statistics and export features
4. Consider setting up multiple API keys for different SDRTrunk instances

## Getting Help

If you're still having trouble:

1. Check the main [README.md](../README.md) for more detailed information
2. Check the [API documentation](API.md) for technical details
3. Look at your log files in the `logs/` directory
4. Open an issue on GitHub with:
   - Your operating system
   - Python version (`python3 --version`)
   - Any error messages
   - Your config.yaml file (remove the API key first!)

Remember: this is designed to be simple! If something seems too complicated, you might be overthinking it.
