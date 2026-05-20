#!/usr/bin/env python3
"""
Discord ↔ pi RPC Bridge Bot

Allows users to interact with a pi coding agent via Discord threads.
Each Discord thread/channel gets its own isolated pi session.

Requirements:
    discord.py>=2.3.0
"""

import asyncio
import logging
import sys
import os
import tempfile
import shutil
import subprocess
from typing import Optional

import discord

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BotConfig, create_default_config
from pi_rpc_client import (
    PiRpcClient,
    MessageDeltaEvent,
    ToolExecutionStartEvent,
    ToolExecutionEndEvent,
    AgentEndEvent,
    ResponseEvent,
)
from session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discord-pi-bot")


class PiDiscordBot(discord.Client):
    """
    Discord bot that bridges messages to pi RPC sessions.
    Uses manual command routing (not discord.py command framework).
    """

    def __init__(self, config: BotConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(intents=intents)

        self.config = config
        self.session_manager = SessionManager(
            pi_command=config.pi_command,
            max_idle_seconds=config.max_session_idle_minutes * 60,
            cleanup_interval=config.session_cleanup_interval_minutes * 60,
        )
        # Per-channel locks to prevent concurrent prompts to the same pi session
        self._channel_locks: dict[str, asyncio.Lock] = {}

    # ── Lifecycle ─────────────────────────────

    async def on_ready(self):
        """Called when the bot is fully connected to Discord."""
        self.session_manager.start()
        await self.change_presence(
            activity=discord.Game(name=self.config.bot_status_message)
        )
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")
        for guild in self.guilds:
            logger.info(f"  - {guild.name} (ID: {guild.id})")

    async def close(self):
        """Clean shutdown."""
        logger.info("Shutting down...")
        self.session_manager.stop()
        await super().close()

    # ── Access Control ────────────────────────

    def _is_allowed_guild(self, guild_id: Optional[int]) -> bool:
        if not self.config.allowed_guild_ids:
            return True
        return str(guild_id) in [str(i) for i in self.config.allowed_guild_ids] if guild_id else False

    def _is_allowed_channel(self, channel_id: Optional[int]) -> bool:
        if not self.config.allowed_channel_ids:
            return True
        return str(channel_id) in [str(i) for i in self.config.allowed_channel_ids] if channel_id else False

    def _is_allowed_user(self, user_id: Optional[int]) -> bool:
        if not self.config.allowed_user_ids:
            return True
        return str(user_id) in [str(i) for i in self.config.allowed_user_ids] if user_id else False

    def _is_admin(self, user_id: Optional[int]) -> bool:
        if not self.config.admin_user_ids:
            return False
        return str(user_id) in [str(i) for i in self.config.admin_user_ids] if user_id else False

    def _get_channel_lock(self, channel_id: str) -> asyncio.Lock:
        """Get or create a per-channel asyncio lock to prevent concurrent prompts."""
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    # ── Message Handler ───────────────────────

    async def on_message(self, message: discord.Message):
        """Main message handler — routes commands and prompts."""
        if message.author.bot:
            return

        prefix = self.config.bot_prefix

        # ── Handle commands ──
        if message.content.startswith(prefix):
            cmd = message.content[len(prefix):].strip()
            if cmd:
                await self._route_command(message, cmd)
            return

        # ── Handle prompts ──
        if not message.guild:
            # DM
            if not self._is_allowed_user(message.author.id):
                logger.info(f"Denied DM: user {message.author.id} not allowed")
                return
            await self._handle_prompt(message)
            return

        # Guild: only respond in threads
        if not isinstance(message.channel, discord.Thread):
            return

        parent = message.channel.parent
        if not parent or not self._is_allowed_channel(parent.id):
            logger.info(f"Denied: thread parent {parent.id if parent else '?'} not allowed")
            return
        if not self._is_allowed_user(message.author.id):
            logger.info(f"Denied: user {message.author.id} not allowed")
            return
        if not self._is_allowed_guild(message.guild.id):
            logger.info(f"Denied: guild {message.guild.id} not allowed")
            return

        await self._handle_prompt(message)

    async def _route_command(self, message: discord.Message, cmd_text: str):
        """Route a command to the right handler."""
        parts = cmd_text.split(maxsplit=1)
        cmd_name = parts[0].lower()
        cmd_args = parts[1] if len(parts) > 1 else ""

        handlers = {
            "help": self._cmd_help,
            "ping": self._cmd_ping,
            "stats": self._cmd_stats,
            "model": self._cmd_model,
            "models": self._cmd_model,
            "new": self._cmd_new,
            "abort": self._cmd_abort,
            "steer": self._cmd_steer,
            "reload": self._cmd_reload,
            "config": self._cmd_config,
            "sessions": self._cmd_sessions,
            "session-kill": self._cmd_session_kill,
            "sk": self._cmd_session_kill,
        }

        handler = handlers.get(cmd_name)
        if handler:
            await handler(message, cmd_args)
        else:
            await message.channel.send(
                f"❌ Unknown command `{self.config.bot_prefix}{cmd_name}`."
                f" Try `{self.config.bot_prefix}help`"
            )

    # ── Prompt Handling ───────────────────────

    async def _handle_prompt(self, message: discord.Message):
        """Send a user message to pi and return the response.
        
        Uses a per-channel lock to ensure prompts to the same pi session
        are processed sequentially (prevents "Done (no output)" errors).
        """
        channel = message.channel
        user = message.author
        prompt_text = message.content

        guild_name = message.guild.name if message.guild else "DM"
        logger.info(f"Prompt from {user} in {channel.name} ({guild_name}): {prompt_text[:80]}...")

        # Acquire per-channel lock to prevent concurrent prompts
        lock = self._get_channel_lock(str(channel.id))
        async with lock:
                # ── Process attachments ────────────────
            file_refs = []
            rpc_images = []
            temp_dir = None
            image_metadata = []

            if message.attachments:
                temp_dir = tempfile.mkdtemp(prefix="pi-discord-")

                for att in message.attachments:
                    data = await att.read()
                    ext = os.path.splitext(att.filename)[1].lower()
                    is_image = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                    file_path = os.path.join(temp_dir, att.filename)

                    with open(file_path, "wb") as f:
                        f.write(data)

                    file_refs.append({
                        "path": file_path,
                        "filename": att.filename,
                        "size": len(data),
                        "is_image": is_image,
                    })
                    logger.info(f"{'📷' if is_image else '📎'} Attachment: {att.filename} ({len(data)} bytes)")

                    # For images: add to RPC images AND extract local metadata
                    if is_image:
                        import base64
                        b64 = base64.b64encode(data).decode("utf-8")
                        mime = {
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".gif": "image/gif",
                            ".webp": "image/webp",
                        }.get(ext, "image/png")
                        rpc_images.append({
                            "type": "image",
                            "data": b64,
                            "mimeType": mime,
                        })
                        # Also extract metadata locally (fallback for non-vision models)
                        meta = self._get_image_metadata(file_path, att.filename)
                        if meta:
                            image_metadata.append(meta)
                            logger.info(f"RPC image added for pi: {att.filename} ({len(b64)} base64 chars)")

            # Append metadata to prompt text (for pi's context)
            if image_metadata:
                prompt_text += "\n\n[Image metadata for attached images:]\n"
                for m in image_metadata:
                    # Extract just the metadata part, skip the decorative text
                    prompt_text += m + "\n"

            # Build prompt with file references (for models without vision)
            if file_refs:
                ref_lines = ["\n📎 **Attached files:**"]
                for ref in file_refs:
                    icon = "📷" if ref["is_image"] else "📄"
                    ref_lines.append(f"- {icon} `{ref['path']}` ({ref['filename']}, {ref['size']} bytes)")

                if any(r["is_image"] for r in file_refs):
                    ref_lines.append(
                        "\nIf you can see attached images visually, describe them. "
                        "Otherwise use `identify` or `file` for metadata."
                    )

                prompt_text += "\n" + "\n".join(ref_lines)

            # ── Send to pi ──
            session = self.session_manager.get_or_create(str(channel.id))
            pi = session.client

            async with channel.typing():
                full_response = ""
                tool_notifications = []

                def on_delta(delta: str):
                    nonlocal full_response
                    full_response += delta

                def on_tool_start(name: str, args: dict):
                    if self.config.show_tool_notifications:
                        tool_notifications.append(f"🔧 Running `{name}`...")

                loop = asyncio.get_event_loop()

                def run_prompt():
                    return pi.prompt_sync(
                        prompt_text,
                        timeout=300,
                        on_delta=on_delta,
                        on_tool=on_tool_start,
                        images=rpc_images or None,
                    )

                try:
                    result_text = await loop.run_in_executor(None, run_prompt)
                    if result_text:
                        full_response = result_text
                        logger.info(f"Pi response: {len(full_response)} chars")
                    else:
                        logger.info(f"Pi returned empty response (model may have only produced thinking blocks)")
                except Exception as e:
                    logger.error(f"Pi error: {e}")
                    await channel.send(f"❌ Error: {e}")
                    return

            # ── Send response ──
            if not full_response and not tool_notifications:
                await channel.send("✅ Done (no output)")
                return

            if tool_notifications:
                for note in tool_notifications:
                    await channel.send(note)

            if full_response:
                max_len = self.config.max_discord_message_length
                chunks = [full_response[i: i + max_len] for i in range(0, len(full_response), max_len)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await channel.send(chunk)
                    else:
                        await channel.send(f"*(continued)*\n{chunk}")

            # Cleanup temp files
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _get_image_metadata(file_path: str, filename: str) -> Optional[str]:
        """Extract image metadata using identify + file commands."""
        meta_lines = []

        try:
            result = subprocess.run(
                ["identify", "-verbose", file_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    stripped = line.strip()
                    if any(k in stripped.lower() for k in [
                        "image:", "geometry", "type:", "depth",
                        "channel depth", "image statistics",
                        "format:", "class:", "filesize"
                    ]):
                        meta_lines.append(stripped)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        try:
            result = subprocess.run(
                ["file", file_path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                meta_lines.insert(0, result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if meta_lines:
            lines = ["📷 **Image: `{}`**".format(filename)]
            lines.append("```")
            lines.extend(meta_lines[:10])
            lines.append("```")
            lines.append(
                "📷 Image metadata extracted (visible to vision-capable models)."
            )
            return "\n".join(lines)
        return None

    # ══════════════════════════════════════════
    # COMMAND HANDLERS
    # ══════════════════════════════════════════

    async def _cmd_help(self, message: discord.Message, args: str):
        """Show help information."""
        p = self.config.bot_prefix
        embed = discord.Embed(
            title="🤖 Discord ↔ pi Agent Bridge",
            description=(
                "📌 **In a server:** Create a **thread** in an allowed channel "
                "and send a message. The bot will respond inside that thread.\n\n"
                "📌 **Direct Messages:** DM the bot directly.\n\n"
                "Each thread / DM gets its own isolated pi session with full context."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Commands",
            value=(
                f"`{p}help` — Show this message\n"
                f"`{p}model` — List / switch models\n"
                f"`{p}sessions` — List active sessions\n"
                f"`{p}session-kill <id>` — Remove a session\n"
                f"`{p}stats` — Show session stats\n"
                f"`{p}new` — Start a fresh session in this channel\n"
                f"`{p}abort` — Abort the current operation\n"
                f"`{p}steer <msg>` — Queue a steering message\n"
                f"`{p}ping` — Check if the bot is alive"
            ),
            inline=False,
        )
        embed.add_field(
            name="Admin Commands",
            value=(
                f"`{p}reload` — Reload config\n"
                f"`{p}config` — Show current config\n"
            ),
            inline=False,
        )
        await message.channel.send(embed=embed)

    async def _cmd_ping(self, message: discord.Message, args: str):
        """Check if the bot is alive."""
        await message.channel.send("🏓 Pong!")

    async def _cmd_stats(self, message: discord.Message, args: str):
        """Show session statistics."""
        stats = self.session_manager.get_stats()
        embed = discord.Embed(
            title="📊 Session Stats",
            color=discord.Color.green(),
        )
        embed.add_field(name="Active Sessions", value=str(stats["active_sessions"]))
        for s in stats["sessions"]:
            embed.add_field(
                name=f"Session: {s['id'][:10]}...",
                value=f"Messages: {s['messages']} | Idle: {s['idle_seconds']}s",
                inline=False,
            )
        if not stats["sessions"]:
            embed.add_field(name="No sessions", value="No active sessions", inline=False)
        await message.channel.send(embed=embed)

    async def _cmd_new(self, message: discord.Message, args: str):
        """Start a fresh pi session in this channel."""
        self.session_manager.remove(str(message.channel.id))
        await message.channel.send("✅ Started a fresh session! Previous context cleared.")

    async def _cmd_abort(self, message: discord.Message, args: str):
        """Abort the current pi operation."""
        try:
            session = self.session_manager.get_or_create(str(message.channel.id))
            session.client.abort()
            await message.channel.send("⏹️ Operation aborted.")
        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")

    async def _cmd_steer(self, message: discord.Message, args: str):
        """Queue a steering message."""
        if not args:
            await message.channel.send(f"❌ Usage: `{self.config.bot_prefix}steer <message>`")
            return
        try:
            session = self.session_manager.get_or_create(str(message.channel.id))
            session.client.send_steer(args)
            await message.channel.send(f"🔄 Steering: {args[:100]}...")
        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")

    async def _cmd_reload(self, message: discord.Message, args: str):
        """Reload config and restart all sessions (admin only)."""
        if not self._is_admin(message.author.id):
            await message.channel.send("❌ Admin only.")
            return
        try:
            new_config = BotConfig.load()
            self.config = new_config
            self.session_manager.stop()
            self.session_manager = SessionManager(
                pi_command=new_config.pi_command,
                max_idle_seconds=new_config.max_session_idle_minutes * 60,
            )
            self.session_manager.start()
            await message.channel.send("✅ Config reloaded, sessions restarted.")
        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")

    async def _cmd_config(self, message: discord.Message, args: str):
        """Show current configuration (admin only)."""
        if not self._is_admin(message.author.id):
            await message.channel.send("❌ Admin only.")
            return
        cfg = self.config.to_dict()
        lines = ["**Config:**"]
        for key, value in cfg.items():
            if isinstance(value, list) and len(value) > 5:
                display = str(value[:5]) + f"... ({len(value)} total)"
            else:
                display = str(value)
            lines.append(f"• `{key}`: {display}")
        await message.channel.send("\n".join(lines))

    async def _cmd_sessions(self, message: discord.Message, args: str):
        """List all active pi sessions."""
        stats = self.session_manager.get_stats()
        if stats["active_sessions"] == 0:
            await message.channel.send("📭 No active sessions.")
            return

        lines = [f"**Active Sessions: {stats['active_sessions']}**"]
        for s in stats["sessions"]:
            idle_str = f"{s['idle_seconds']:.0f}s" if s['idle_seconds'] < 120 else f"{s['idle_seconds']/60:.0f}m"
            lines.append(
                f"• `{s['id']}` — {s['messages']} msgs, idle {idle_str}"
            )
        lines.append(f"\nUse `{self.config.bot_prefix}session-kill <id>` to remove a session.")
        await message.channel.send("\n".join(lines))

    async def _cmd_session_kill(self, message: discord.Message, args: str):
        """Remove a specific pi session by channel/thread ID."""
        if not args:
            await message.channel.send(
                f"❌ Usage: `{self.config.bot_prefix}session-kill <channel_id>`\n"
                f"   Use `{self.config.bot_prefix}sessions` to list active session IDs."
            )
            return
        channel_id = args.strip()
        self.session_manager.remove(channel_id)
        await message.channel.send(f"✅ Removed session for channel `{channel_id}`.")
        logger.info(f"Session killed by user: {channel_id}")

    async def _cmd_model(self, message: discord.Message, args: str):
        """List available models or switch to a specific one."""
        channel = message.channel

        # Get or create session
        try:
            session = self.session_manager.get_or_create(str(channel.id))
            pi = session.client
        except Exception as e:
            await channel.send(f"❌ Error: {e}")
            return

        loop = asyncio.get_event_loop()

        # Fetch models
        def fetch_models():
            return pi.get_available_models()
        models = await loop.run_in_executor(None, fetch_models)
        if not models:
            await channel.send("❌ No models available.")
            return

        # Get current state
        def fetch_state():
            return pi.get_state()
        state = await loop.run_in_executor(None, fetch_state)

        current_key = ""
        if state and state.get("model"):
            m = state["model"]
            current_key = f"{m.get('provider','?')}/{m.get('id','?')}"

        # ── No args: list models ──
        if not args:
            lines = [f"**Available Models** (current: `{current_key}`)"]
            for i, m in enumerate(models, 1):
                key = f"{m.get('provider','?')}/{m.get('id','?')}"
                marker = "✅" if key == current_key else "⬜"
                vision = " 📷" if "image" in m.get("input", []) else ""
                lines.append(f"`{i}.` {marker} `{m['id']}`{vision}")
            lines.append(f"\nUse `{self.config.bot_prefix}model <name>` to switch.")
            lines.append("")
            lines.append("💡 **Vision models** (can describe images): `kimi-k2.5`, `kimi-k2.6`, `gemma4`")
            await channel.send("\n".join(lines))
            return

        # ── Find model by name ──
        selected = None
        name_lower = args.lower()
        for m in models:
            if name_lower in m["id"].lower():
                selected = m
                break
        if not selected:
            await channel.send(f"❌ No model matching `{args}`")
            return

        # ── Switch model ──
        provider, model_id = selected["provider"], selected["id"]
        await channel.send(f"🔄 Switching to `{model_id}`...")

        def do_switch():
            pi.set_model(provider, model_id)
            return pi.get_state()
        try:
            await loop.run_in_executor(None, do_switch)
            await channel.send(f"✅ Now using **{model_id}**!")
            logger.info(f"Model switched to {provider}/{model_id} in channel {channel.id}")
        except Exception as e:
            await channel.send(f"❌ Switch failed: {e}")


# ── Main ──────────────────────────────────────

def main():
    config_path = os.environ.get(
        "PI_DISCORD_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
    )
    create_default_config(config_path)

    config = BotConfig.load(config_path)

    if not config.token:
        print(
            "❌ No Discord bot token found.\n"
            "   Set the DISCORD_BOT_TOKEN environment variable or\n"
            f"   edit the token field in {config_path}"
        )
        sys.exit(1)

    bot = PiDiscordBot(config)
    logger.info("Starting bot...")
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
