# Discord ↔ pi Agent Bridge

Chat with pi coding agents through Discord threads. Each thread gets its own isolated `pi --mode rpc` session.

## Quick Start

### 1. Install dependencies
```bash
cd <pi-discord-bridge-directory>
pip install -r requirements.txt
```

### 2. Configure
Edit `config.json` with your Discord bot token and IDs:
```json
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "allowed_guild_ids": ["YOUR_SERVER_ID"],
  "allowed_user_ids": ["YOUR_USER_ID"],
  "admin_user_ids": ["YOUR_USER_ID"]
}
```

Or use environment variables:
```bash
export DISCORD_BOT_TOKEN="your_token"
export DISCORD_GUILD_ID="your_guild_id"
```

### 3. Run
```bash
python3 bot.py
```

Or use the manager:
```bash
bash pi-discord-manager.sh
```

### 4. Invite the bot to your server
1. Go to Discord Developer Portal → OAuth2 → URL Generator
2. Select scopes: `bot`
3. Select permissions: Send Messages, Send Messages in Threads, Create Threads, Read Message History, View Channels
4. Open the generated URL

## Usage

Send a message in a thread or DM the bot. Each thread gets its own pi session.

### Commands
- `!help` — Show all commands
- `!model` — List/switch pi models (use `kimi-k2.5` for vision/image analysis)
- `!sessions` — List active pi sessions
- `!session-kill <id>` — Remove a session
- `!new` — Reset session context
- `!abort` — Cancel current operation
- `!steer <msg>` — Queue a steering message
- `!stats` — Show session statistics

### Image Support
- Vision models (`kimi-k2.5`, `kimi-k2.6`, `gemma4`) can describe images
- Non-vision models get image metadata via ImageMagick
- Switch with: `!model kimi-k2.5`

## Architecture
```
Discord → Python Bot → pi --mode rpc (subprocess per thread)
```
