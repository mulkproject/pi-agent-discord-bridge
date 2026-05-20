# Discord Bridge Setup

Help the user set up the Discord ↔ pi Bridge bot.

## Steps

1. Ensure Python 3.10+ is installed
2. Install required packages:
   ```bash
   cd <package-directory>
   pip install -r requirements.txt
   ```
3. Guide the user to create a Discord bot at https://discord.com/developers/applications
   - Enable Message Content Intent
   - Copy the bot token
4. Help configure `config.json` with their token, guild ID, channel ID, and user ID
5. Start the bot:
   ```bash
   python3 bot.py
   # or
   bash pi-discord-manager.sh
   ```
6. Invite the bot to their server using the OAuth2 URL Generator with `bot` scope and appropriate permissions

## Troubleshooting

- "CommandNotFound" → The bot was restarted. Use `!help` to see available commands.
- Bot doesn't respond → Check `allowed_channel_ids`, `allowed_user_ids`, and `allowed_guild_ids` in config.
- Image not visible → Switch to a vision model: `!model kimi-k2.5`
