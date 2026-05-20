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
- **Auto-cleanup** — Idle sessions are automatically terminated after 30 minutes
- **Access control** — Restrict by guild, channel, and user ID
- **Streaming responses** — See pi's output as it's generated
- **Tool notifications** — See when pi runs tools (bash, read, edit, write)
- **Admin commands** — Reload config, view stats, reset sessions

## Requirements

- Python 3.10+
- [pi](https://pi.dev) installed and available in PATH (`npm install -g @earendil-works/pi-coding-agent`)
- A Discord bot token

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** and give it a name
3. Go to **Bot** → **Add Bot**
4. Under the **Privileged Gateway Intents** section, enable:
   - ✅ **Message Content Intent** (required to read messages)
   - ✅ **Server Members Intent** (optional, but recommended)
5. Copy the bot **Token**

### 2. Invite the Bot to Your Server

1. In the Developer Portal, go to **OAuth2** → **URL Generator**
2. Select scopes: `bot` `applications.commands`
3. Select permissions:
   - `Send Messages`
   - `Send Messages in Threads`
   - `Create Public Threads`
   - `Create Private Threads`
   - `Read Message History`
   - `View Channels`
4. Open the generated URL in your browser to invite the bot

### 3. Install and Configure

```bash
# Navigate to the bot directory
cd discord-pi-bot

# Install Python dependencies
pip install -r requirements.txt

# Edit config.json with your settings
nano config.json
```

### 4. Configuration

Edit `config.json`:

```json
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "allowed_guild_ids": ["123456789012345678"],
  "allowed_channel_ids": [],
  "allowed_user_ids": ["987654321098765432"],
  "admin_user_ids": ["987654321098765432"],
  "pi_command": "pi",
  "max_session_idle_minutes": 30,
  "show_tool_notifications": true
}
```

| Field | Description |
|-------|-------------|
| `token` | Discord bot token (or set `DISCORD_BOT_TOKEN` env var) |
| `allowed_guild_ids` | Server IDs where the bot should respond (empty = all servers) |
| `allowed_channel_ids` | Channel/thread IDs to monitor (empty = all channels in allowed guilds) |
| `allowed_user_ids` | User IDs allowed to talk to the bot (empty = all users) |
| `admin_user_ids` | User IDs allowed to run admin commands |
| `pi_command` | Path to the pi binary (usually just `pi`) |
| `max_session_idle_minutes` | Auto-terminate idle sessions after this many minutes |
| `show_tool_notifications` | Post 🔧 notifications when pi runs a tool |

### 5. Run

```bash
python bot.py
```

Or with environment variables:

```bash
export DISCORD_BOT_TOKEN="your_token_here"
export DISCORD_GUILD_ID="your_guild_id"
export DISCORD_CHANNEL_ID="your_channel_id"
python bot.py
```

## Usage

### Basic Interaction

1. Create a **thread** in a monitored channel (or use a monitored channel directly)
2. Send a message in the thread
3. The bot processes it through pi and responds

Each thread maintains its own independent pi session with full context history.

### Commands

| Command | Description |
|---------|-------------|
| `!help` | Show help message |
| `!stats` | Show session statistics |
| `!new` | Start a fresh pi session in this channel (clears context) |
| `!abort` | Abort the current running operation |
| `!steer <message>` | Queue a steering message during an active operation |
| `!ping` | Check if the bot is alive |
| `!reload` | Reload config and restart all sessions (admin only) |
| `!config` | Show current configuration (admin only) |

### Access Control Examples

**Allow a specific channel only:**
```json
{
  "allowed_guild_ids": ["123456789012345678"],
  "allowed_channel_ids": ["123456789012345678"]
}
```

**Allow specific users only:**
```json
{
  "allowed_user_ids": ["987654321098765432", "987654321098765433"]
}
```

**Allow a public bot (everyone in allowed channels):**
```json
{
  "allowed_guild_ids": ["123456789012345678"],
  "allowed_user_ids": []
}
```

## How It Works

1. User sends a message in a Discord thread
2. Bot checks access control rules
3. Bot gets or creates a `pi --mode rpc` process for that thread
4. Bot sends the message as a JSON-RPC `prompt` command
5. Pi processes the prompt, streaming back events (text deltas, tool calls, etc.)
6. Bot streams the response back to Discord in real-time
7. The pi process stays alive for follow-up messages in the same thread

## Project Structure

```
discord-pi-bot/
├── bot.py              # Main Discord bot with commands
├── pi_rpc_client.py    # Pi RPC subprocess manager
├── session_manager.py  # Per-channel session lifecycle
├── config.py           # Configuration loader
├── config.json         # Bot configuration
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

## Troubleshooting

**Bot doesn't respond:**
- Check that the bot has `Message Content Intent` enabled
- Verify the bot's permissions (Send Messages, Read Message History)
- Check console logs for access control denials
- Verify your token is correct

**Pi not starting:**
- Run `which pi` to verify pi is installed
- Run `pi --mode rpc --no-session` manually to test
- Set `pi_command` in config if pi is not in PATH

**Session errors:**
- Use `!new` to reset the session for a channel
- Check pi's API key is set (`ANTHROPIC_API_KEY`, etc.)

## License

MIT
