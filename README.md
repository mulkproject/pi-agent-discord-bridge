# Discord ↔ pi Agent Bridge

A Python Discord bot that bridges messages to [pi](https://pi.dev) coding agents via pi's RPC mode.

Each Discord thread or channel gets its own isolated pi session with full context, so you can have
multiple concurrent conversations with different agents, each with their own working memory.

## Architecture

```
Discord ──► Python Discord Bot ──► pi --mode rpc (subprocess per channel/thread)
                ▲                        │
                └── responses / events ◄─┘
```

## Features

- **Per-thread sessions** — Each Discord thread gets its own `pi --mode rpc` process
- **Auto-cleanup** — Idle sessions auto-terminated after 30 minutes
- **Access control** — Restrict by guild, channel, and user ID
- **Streaming responses** — See pi's output in real-time
- **Image support** — Vision models can describe images; metadata fallback for others
- **Model switching** — Switch pi models directly from Discord (`!model`)
- **TUI Manager** — Terminal UI to start, stop, monitor, and manage the bot
- **Single-instance lock** — Prevents multiple bot processes from running
- **Autostart** — Systemd service for boot-time launch

## Requirements

- Python 3.10+
- [pi](https://pi.dev) installed and in PATH (`npm install -g @earendil-works/pi-coding-agent`)
- A Discord bot token

## Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Run the TUI manager
bash pi-discord-manager.sh
```

The TUI manager gives you a menu to start, stop, and monitor the bot.

## TUI Manager (`pi-discord-manager.sh`)

The **Terminal User Interface** manager handles all backend operations. Use it to control the bot service.

```bash
# Interactive menu
bash pi-discord-manager.sh

# Or direct commands:
bash pi-discord-manager.sh start     # Start the bot (single-instance enforced)
bash pi-discord-manager.sh stop      # Stop the bot gracefully
bash pi-discord-manager.sh restart   # Restart the bot
bash pi-discord-manager.sh status    # Show PID, uptime, memory, session count
bash pi-discord-manager.sh logs      # Tail live logs (Ctrl+C to exit)
bash pi-discord-manager.sh sessions  # List active pi RPC processes
bash pi-discord-manager.sh deps      # Check all dependencies
sudo bash pi-discord-manager.sh autostart  # Install systemd service (boot auto-start)
```

### Interactive Menu Options

| Option | Action |
|--------|--------|
| `1` | Start Bot |
| `2` | Stop Bot |
| `3` | Restart Bot |
| `4` | Show Status (PID, uptime, memory, sessions) |
| `5` | View Live Logs |
| `6` | Configure Autostart (systemd) |
| `7` | Check Dependencies |
| `8` | List Active pi Sessions |
| `0` | Exit |

### Single-Instance Safety

The manager uses a PID file at `/tmp/pi-discord-bot.pid` to prevent multiple bot instances.
If you try to start the bot twice, it will warn you and refuse.

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → give it a name
3. Go to **Bot** → **Add Bot**
4. Enable **Privileged Gateway Intents**:
   - ✅ **Message Content Intent** (required)
   - ✅ **Server Members Intent** (recommended)
5. Copy the bot **Token**

### 2. Invite the Bot to Your Server

1. Go to **OAuth2** → **URL Generator**
2. Select scopes: `bot`
3. Select permissions:
   - `Send Messages`
   - `Send Messages in Threads`
   - `Create Public Threads`
   - `Create Private Threads`
   - `Read Message History`
   - `View Channels`
4. Open the generated URL in your browser

### 3. Configure

```bash
# Create config.json from the template (run this once)
python3 -c "from config import create_default_config; create_default_config()"

# Edit with your settings
nano config.json
```

Edit `config.json`:

```json
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "allowed_guild_ids": ["YOUR_SERVER_ID"],
  "allowed_channel_ids": ["YOUR_CHANNEL_ID"],
  "allowed_user_ids": ["YOUR_USER_ID"],
  "admin_user_ids": ["YOUR_USER_ID"]
}
```

| Field | Description |
|-------|-------------|
| `token` | Discord bot token (or set `DISCORD_BOT_TOKEN` env var) |
| `allowed_guild_ids` | Server IDs to allow (empty = all) |
| `allowed_channel_ids` | Channel/thread IDs to monitor (empty = all) |
| `allowed_user_ids` | User IDs allowed to chat (empty = all) |
| `admin_user_ids` | User IDs allowed to run admin commands |
| `pi_command` | Path to pi binary (default: `pi`) |
| `max_session_idle_minutes` | Auto-terminate idle sessions (default: 30) |
| `show_tool_notifications` | Show 🔧 tool notifications (default: true) |

You can also use environment variables:
```bash
export DISCORD_BOT_TOKEN="your_token"
export DISCORD_GUILD_ID="your_guild_id"
export DISCORD_CHANNEL_ID="your_channel_id"
```

### 4. Run

```bash
# Using the TUI manager (recommended):
bash pi-discord-manager.sh
# Then select option 1 to start

# Or directly:
python3 bot.py

# With autostart on boot:
sudo bash pi-discord-manager.sh autostart
```

## Usage

### Discord Commands

| Command | Description |
|---------|-------------|
| `!help` | Show all commands |
| `!model` | List available pi models |
| `!model <name>` | Switch to a specific model (e.g., `!model kimi-k2.5`) |
| `!sessions` | List active pi sessions |
| `!session-kill <id>` | Remove a specific session |
| `!new` | Reset session context in this thread |
| `!abort` | Cancel the current operation |
| `!steer <message>` | Queue a steering message |
| `!ping` | Check if the bot is alive |
| `!stats` | Show session statistics |
| `!reload` | Reload config (admin only) |
| `!config` | Show current config (admin only) |

### Basic Interaction

1. Create a **thread** in an allowed channel
2. Send a message — the bot responds via pi
3. Each thread = isolated pi session

### Image Support

- **Vision models** (`kimi-k2.5`, `kimi-k2.6`, `gemma4`) can describe image content
- **Non-vision models** get image metadata extracted locally (dimensions, format, type)
- Switch models with: `!model kimi-k2.5`

## How It Works

1. User sends a message in a Discord thread
2. Bot checks access control rules
3. Bot spawns (or reuses) a `pi --mode rpc` subprocess for that thread
4. Bot sends the message as a JSON-RPC `prompt` command (with images if attached)
5. Pi processes the prompt, streaming back events
6. Bot streams the response back to Discord in real-time
7. Idle sessions auto-cleanup after 30 minutes

## Project Structure

```
pi-discord-bridge/
├── bot.py                  # Main Discord bot
├── pi_rpc_client.py        # Pi RPC subprocess manager
├── session_manager.py      # Per-channel session lifecycle + cleanup
├── config.py               # Configuration loader
├── config.json             # ⚠️ Your Discord token + IDs (gitignored)
├── pi-discord-manager.sh   # 🆕 TUI manager (start/stop/status/logs/autostart)
├── package.json            # Pi package manifest
├── requirements.txt        # Python dependencies
├── README.md               # This file
├── .gitignore              # Excludes config.json from git
├── skills/
│   └── SKILL.md            # Pi skill: setup guide
└── prompts/
    └── setup.md            # Pi prompt template: setup walkthrough
```

## Troubleshooting

**Bot doesn't respond:**
- Check `Message Content Intent` is enabled in Discord Developer Portal
- Verify `allowed_user_ids`, `allowed_channel_ids`, `allowed_guild_ids` in config
- Check logs: `bash pi-discord-manager.sh logs`

**!model says "Unknown command":**
- The bot was just restarted — all previous output was from an older version
- Try again now with the latest code

**Image not visible:**
- Switch to a vision model: `!model kimi-k2.5`
- Check the logs: `bash pi-discord-manager.sh logs`

**Pi not starting:**
- Run `which pi` to verify pi is installed
- Test manually: `pi --mode rpc --no-session`
- Check pi's API key is set

## Install as a Pi Package

```bash
pi install git:github.com/mulkproject/pi-agent-discord-bridge
```

This installs the SKILL.md setup guide and prompt template.
The bot itself runs as a separate Python process.

## License

MIT
