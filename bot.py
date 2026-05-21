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
import time
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
        # Per-channel working directories (like "cd" for pi sessions)
        self._channel_cwds: dict[str, str] = {}
        # Per-channel TTS toggle (bot reads responses aloud)
        self._channel_tts: dict[str, bool] = {}

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

        # ── Handle voice messages (always on) ──
        audio_attachment = None
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("audio/"):
                audio_attachment = att
                break

        if audio_attachment:
            logger.info(f"Voice message detected: {audio_attachment.filename} ({audio_attachment.size} bytes)")
            # Download the audio file
            temp_dir = tempfile.mkdtemp(prefix="pi-voice-")
            audio_path = os.path.join(temp_dir, audio_attachment.filename)
            await audio_attachment.save(audio_path)
            # Transcribe
            transcribed = await self._transcribe_audio(audio_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            if transcribed:
                # Prepend transcription to user's message
                message.content = f"[Voice message transcribed: \"{transcribed}\"]\n\n{message.content}"
                logger.info(f"Voice transcribed: {transcribed[:100]}")
            else:
                await message.channel.send("🎤 I heard your voice message but couldn't understand it. Please try again or type instead.")
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
            "cd": self._cmd_cd,
            "pwd": self._cmd_pwd,
            "cwd": self._cmd_pwd,
            "screenshot": self._cmd_screenshot,
            "ss": self._cmd_screenshot,
            "tts": self._cmd_tts,
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

            # Get channel cwd for system context
            channel_id = str(channel.id)
            cwd = self._channel_cwds.get(channel_id)

            # Add system capabilities to prompt (so pi knows what tools are available)
            screenshot_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshot.py")
            prompt_text += (
                "\n\n[System capabilities:]\n"
                f"- Screenshots: Use `python3 {screenshot_script} <url>` via bash to take webpage screenshots.\n"
                "  Screenshots will be automatically sent to the Discord user.\n"
                f"- Working directory: {cwd or os.getcwd()}\n"
            )

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
            session = self.session_manager.get_or_create(channel_id, cwd=cwd)
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

                    # ── Auto-continue: keep agent working until task is done ──
                    max_continue = 3
                    continue_count = 0
                    while continue_count < max_continue:
                        text = full_response or ""
                        last_200 = text[-200:] if len(text) > 200 else text
                        needs_continue = False

                        # Check for incomplete patterns
                        for kw in ["Let me", "I'll", "Now I", "I will", "Next,", "Next step",
                                   "Continuing", "To finish", "Finally,", "Moving on",
                                   "I should", "I need to", "Let's", "We need to",
                                   "The next", "After that", "Once that", "Then I'll",
                                   "I'm going to", "I plan to", "I'd like to"]:
                            if kw in last_200:
                                needs_continue = True
                                break

                        # Check if tool notifications show writes but no completion
                        if not needs_continue and tool_notifications:
                            recent = tool_notifications[-3:]
                            has_write = any("write" in t or "edit" in t for t in recent)
                            has_done = any(kw in text.lower()[-500:] for kw in ["done", "complete", "finished", "wrote", "created", "updated"])
                            if has_write and not has_done:
                                needs_continue = True

                        if not needs_continue:
                            break

                        continue_count += 1
                        logger.info(f"Auto-continue #{continue_count}: sending follow-up")

                        def run_continue():
                            return pi.prompt_sync(
                                "Continue and finish the task completely. Keep working until it's fully done.",
                                timeout=300, on_delta=on_delta, on_tool=on_tool_start,
                            )

                        extra = await loop.run_in_executor(None, run_continue)
                        if extra:
                            full_response += "\n\n---\n\n" + extra
                            logger.info(f"Auto-continue #{continue_count}: +{len(extra)} chars")
                        else:
                            break

                except Exception as e:
                    logger.error(f"Pi error: {e}")
                    await channel.send(f"❌ Error: {e}")
                    return

            # ── Check for screenshot files created by pi ──
            screenshot_files = []
            if temp_dir:
                for fname in os.listdir(temp_dir):
                    if fname.endswith(".png") and "screenshot" in fname.lower():
                        fpath = os.path.join(temp_dir, fname)
                        screenshot_files.append(fpath)
            # Also check /tmp for recent pi screenshot files
            for root, dirs, files in os.walk("/tmp"):
                for fname in files:
                    if fname.endswith(".png") and "screenshot" in fname.lower():
                        fpath = os.path.join(root, fname)
                        # Check if created in last 30 seconds
                        age = time.time() - os.path.getctime(fpath)
                        if age < 30:
                            screenshot_files.append(fpath)
                            break
                if screenshot_files:
                    break
            screenshot_files = list(set(screenshot_files))[:3]  # Max 3

            # ── Send response ──
            if not full_response and not tool_notifications and not screenshot_files:
                await channel.send("✅ Done (no output)")
                return

            if tool_notifications:
                # Batch tool notifications into a single compact summary
                from collections import Counter
                tool_counts = Counter()
                for note in tool_notifications:
                    name = note.replace("🔧 Running `", "").replace("`...", "")
                    tool_counts[name] += 1

                summary_parts = []
                for tool, count in tool_counts.most_common():
                    icon = {"read": "📖", "write": "✏️", "edit": "✂️", "bash": "💻", "ls": "📂", "grep": "🔍", "find": "🔎"}.get(tool, "🔧")
                    if count == 1:
                        summary_parts.append(f"{icon} `{tool}`")
                    else:
                        summary_parts.append(f"{icon} `{tool}` x{count}")

                summary = "🔧 **Tools used:** " + ", ".join(summary_parts)
                await channel.send(summary)

            # Send screenshot files if pi created any
            if screenshot_files:
                for fpath in screenshot_files:
                    try:
                        fname = os.path.basename(fpath)
                        await channel.send(file=discord.File(fpath, filename=fname))
                        logger.info(f"Sent screenshot: {fpath}")
                    except Exception as e:
                        logger.error(f"Failed to send screenshot {fpath}: {e}")

            if full_response:
                max_len = self.config.max_discord_message_length
                chunks = [full_response[i: i + max_len] for i in range(0, len(full_response), max_len)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await channel.send(chunk)
                    else:
                        await channel.send(f"*(continued)*\n{chunk}")

                # Generate and send TTS voice clip if enabled
                tts_file = await self._generate_tts(full_response, str(channel.id))
                if tts_file:
                    try:
                        await channel.send(
                            "🔊 **Voice response:**",
                            file=discord.File(tts_file, "response.mp3")
                        )
                        logger.info("TTS voice clip sent to Discord")
                    except Exception as e:
                        logger.error(f"Failed to send TTS: {e}")
                    finally:
                        tts_dir = os.path.dirname(tts_file)
                        shutil.rmtree(tts_dir, ignore_errors=True)

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
                f"`{p}screenshot <url>` — Take a webpage screenshot\n"
                f"`{p}tts on/off` — Toggle voice responses\n"
                f"`{p}model` — List / switch models\n"
                f"`{p}cd <path>` — Set working directory\n"
                f"`{p}pwd` — Show current working directory\n"
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

    async def _cmd_cd(self, message: discord.Message, args: str):
        """Set the working directory for this session (like 'cd' in terminal).
        
        This restarts the pi session so pi operates in the given project directory.
        Pi's tools (read, write, bash) will work relative to this directory.
        
        Usage: !cd /path/to/project
               !cd .    (reset to bot directory)
               !cd ~    (go home)
        """
        if not args:
            current = self._channel_cwds.get(str(message.channel.id), os.getcwd())
            await message.channel.send(f"📂 Current working directory: `{current}`")
            return

        # Resolve the path
        expanded = os.path.expanduser(args)
        if not os.path.isabs(expanded):
            # Relative to current cwd or bot directory
            base = self._channel_cwds.get(str(message.channel.id), os.getcwd())
            expanded = os.path.abspath(os.path.join(base, expanded))

        if not os.path.isdir(expanded):
            await message.channel.send(f"❌ Directory not found: `{expanded}`")
            return

        channel_id = str(message.channel.id)
        self._channel_cwds[channel_id] = expanded

        # Kill existing session so it restarts with new cwd
        self.session_manager.remove(channel_id)

        await message.channel.send(f"📂 Working directory set to: `{expanded}`")
        logger.info(f"Session cwd changed: {channel_id} -> {expanded}")

    async def _cmd_pwd(self, message: discord.Message, args: str):
        """Show the current working directory for this session."""
        channel_id = str(message.channel.id)
        cwd = self._channel_cwds.get(channel_id, os.getcwd())
        await message.channel.send(f"📂 Working directory: `{cwd}`")

    async def _cmd_screenshot(self, message: discord.Message, args: str):
        """Take a screenshot of a URL and send it to Discord.
        
        Usage: !screenshot https://example.com
               !ss https://example.com
        """
        url = args.strip()
        if not url:
            await message.channel.send(
                f"❌ Usage: `{self.config.bot_prefix}screenshot <url>`\n"
                f"   Example: `{self.config.bot_prefix}screenshot https://example.com`"
            )
            return

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        await message.channel.send(f"📸 Taking screenshot of `{url}`...")

        try:
            from playwright.sync_api import sync_playwright

            temp_dir = tempfile.mkdtemp(prefix="pi-screenshot-")
            output_path = os.path.join(temp_dir, "screenshot.png")

            def take_shot():
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    page.goto(url, wait_until="networkidle", timeout=30000)
                    page.screenshot(path=output_path, full_page=False)
                    browser.close()
                return output_path

            loop = asyncio.get_event_loop()
            result_path = await loop.run_in_executor(None, take_shot)

            if os.path.exists(result_path):
                await message.channel.send(
                    f"📸 Screenshot of `{url}`:",
                    file=discord.File(result_path, "screenshot.png")
                )
                logger.info(f"Screenshot taken: {url} -> {result_path}")
            else:
                await message.channel.send("❌ Screenshot failed.")

            shutil.rmtree(temp_dir, ignore_errors=True)

        except ImportError:
            await message.channel.send(
                "❌ Playwright is not installed. Run: pip install playwright && python3 -m playwright install chromium"
            )
        except Exception as e:
            await message.channel.send(f"❌ Screenshot failed: {e}")
            logger.error(f"Screenshot error: {e}")

    async def _cmd_tts(self, message: discord.Message, args: str):
        """Toggle text-to-speech for bot responses.
        
        When enabled, every response will also include a voice clip.
        Usage: !tts on / !tts off
        """
        channel_id = str(message.channel.id)
        action = args.strip().lower()

        if action == "on":
            self._channel_tts[channel_id] = True
            await message.channel.send("🔊 **TTS enabled** — responses will include voice clips.")
            logger.info(f"TTS enabled for channel {channel_id}")
        elif action == "off":
            self._channel_tts[channel_id] = False
            await message.channel.send("🔇 **TTS disabled** — voice clips turned off.")
            logger.info(f"TTS disabled for channel {channel_id}")
        else:
            current = self._channel_tts.get(channel_id, False)
            status = "on" if current else "off"
            await message.channel.send(
                f"🔊 TTS is currently **{status}**.\n"
                f"Use `{self.config.bot_prefix}tts on` or `{self.config.bot_prefix}tts off` to toggle."
            )

    async def _generate_tts(self, text: str, channel_id: str) -> Optional[str]:
        """Generate TTS audio file from text using edge-tts. Returns file path or None.
        
        Strips code blocks, file paths, URLs, and special characters before TTS
        so only natural speech is spoken aloud.
        """
        if not text or not self._channel_tts.get(channel_id, False):
            return None
        try:
            import re
            import edge_tts

            # Clean text for speech: strip code blocks, paths, URLs, special chars
            speech_text = text

            # 1. Remove code blocks (```...```)
            speech_text = re.sub(r'```[\s\S]*?```', '', speech_text)

            # 2. Remove inline code (`...`)
            speech_text = re.sub(r'`[^`]+`', '', speech_text)

            # 3. Remove file paths (/tmp/foo/bar, /home/..., ./path, etc.)
            speech_text = re.sub(r'\/[\w\-\.\/]+\/\w+[\.\w]*', ' ', speech_text)

            # 4. Remove URLs (https://..., http://...)
            speech_text = re.sub(r'https?:\/\/[^\s]+', '', speech_text)

            # 5. Remove markdown links [text](url)
            speech_text = re.sub(r'\[[^\]]+\]\([^)]+\)', '', speech_text)

            # 6. Replace markdown bold/italic with just the text
            speech_text = re.sub(r'\*\*(.+?)\*\*', r'\1', speech_text)
            speech_text = re.sub(r'\*(.+?)\*', r'\1', speech_text)
            speech_text = re.sub(r'__(.+?)__', r'\1', speech_text)

            # 7. Remove excessive whitespace
            speech_text = re.sub(r'\n\s*\n', '\n', speech_text)
            speech_text = re.sub(r' {2,}', ' ', speech_text)

            # 8. Truncate to reasonable length and strip
            speech_text = speech_text.strip()[:800]
            if not speech_text:
                return None

            temp_dir = tempfile.mkdtemp(prefix="pi-tts-")
            output_path = os.path.join(temp_dir, "response.mp3")
            communicate = edge_tts.Communicate(speech_text, voice="en-US-AriaNeural")
            await communicate.save(output_path)
            if os.path.exists(output_path):
                logger.info(f"TTS generated: {len(speech_text)} chars (was {len(text)}) -> {output_path}")
                return output_path
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
        return None

    async def _transcribe_audio(self, audio_path: str) -> Optional[str]:
        """Transcribe audio file to text using SpeechRecognition."""
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio_data = r.record(source)
            text = r.recognize_google(audio_data)
            logger.info(f"Transcribed audio ({os.path.getsize(audio_path)} bytes): {text[:80]}...")
            return text
        except sr.UnknownValueError:
            logger.warning("Could not understand audio")
            return None
        except sr.RequestError as e:
            logger.error(f"STT API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Audio transcription failed: {e}")
            return None

    async def _cmd_model(self, message: discord.Message, args: str):
        """List available models or switch to a specific one."""
        channel = message.channel

        # Get or create session
        try:
            session = self.session_manager.get_or_create(channel_id, cwd=cwd)
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
