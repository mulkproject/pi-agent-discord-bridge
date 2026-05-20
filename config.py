"""
Configuration for the Discord ↔ pi RPC bridge bot.
"""

import json
import os
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("config")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


@dataclass
class BotConfig:
    # ── Discord ───────────────────────────────
    token: str = ""
    """Discord bot token (can also be set via DISCORD_BOT_TOKEN env var)."""

    allowed_guild_ids: list[str] = field(default_factory=list)
    """If non-empty, only respond to threads/channels in these guilds."""

    allowed_channel_ids: list[str] = field(default_factory=list)
    """If non-empty, only respond in these channels/threads."""

    allowed_user_ids: list[str] = field(default_factory=list)
    """If non-empty, only respond to these users."""

    bot_prefix: str = "!"
    """Command prefix for admin commands."""

    admin_user_ids: list[str] = field(default_factory=list)
    """Users who can run admin commands."""

    # ── Pi RPC ────────────────────────────────
    pi_command: str = "pi"
    """Path to the pi CLI binary."""

    pi_extra_args: list[str] = field(default_factory=list)
    """Extra CLI args to pass to pi."""

    pi_environment: dict[str, str] = field(default_factory=dict)
    """Extra environment variables for pi subprocesses."""

    # ── Sessions ──────────────────────────────
    max_session_idle_minutes: int = 30
    """How long a session stays alive without activity before cleanup."""

    session_cleanup_interval_minutes: int = 5
    """How often to check for idle sessions."""

    # ── Discord Messages ──────────────────────
    max_discord_message_length: int = 1900
    """Max characters per Discord message (safety margin under 2000)."""

    typing_indicator: bool = True
    """Show typing indicator while pi is processing."""

    show_tool_notifications: bool = True
    """Post a notification when pi runs a tool."""

    show_thinking: bool = False
    """Show thinking/reasoning blocks from pi (verbose)."""

    # ── Status ────────────────────────────────
    bot_status_message: str = "with pi | /help"
    """Discord bot 'playing' status."""

    def __post_init__(self):
        # Env var overrides
        env_token = os.environ.get("DISCORD_BOT_TOKEN")
        if env_token:
            self.token = env_token

        env_guild = os.environ.get("DISCORD_GUILD_ID")
        if env_guild and not self.allowed_guild_ids:
            self.allowed_guild_ids = [env_guild]

        env_channel = os.environ.get("DISCORD_CHANNEL_ID")
        if env_channel and not self.allowed_channel_ids:
            self.allowed_channel_ids = [env_channel]

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "BotConfig":
        """Load config from a JSON file, with env var overrides."""
        config_path = path
        if not os.path.exists(config_path):
            # Try env var
            config_path = os.environ.get("PI_DISCORD_CONFIG", "")
            if not config_path or not os.path.exists(config_path):
                logger.warning(f"No config file found at {path}")
                logger.info("Using defaults + environment variables")
                return cls()

        logger.info(f"Loading config from: {config_path}")
        with open(config_path) as f:
            data = json.load(f)

        # Convert minutes to seconds internally
        if "max_session_idle_minutes" in data:
            pass  # We keep as minutes in config, convert in code

        return cls(**data)

    def to_dict(self) -> dict:
        """Export config to a dict (sanitized, no token)."""
        d = self.__dict__.copy()
        d.pop("token", None)  # Never dump the token
        return d


def create_default_config(path: str = DEFAULT_CONFIG_PATH):
    """Create a default config.json if it doesn't exist."""
    if os.path.exists(path):
        logger.info(f"Config already exists at {path}")
        return

    default = {
        # ── Required: set these ──
        "token": "",  # Or use DISCORD_BOT_TOKEN env var
        "allowed_guild_ids": [],  # e.g. ["123456789012345678"]
        "allowed_channel_ids": [],  # e.g. ["123456789012345678"] — channels/threads
        "allowed_user_ids": [],  # e.g. ["123456789012345678"]
        "admin_user_ids": [],

        # ── Optional ──
        "pi_command": "pi",
        "pi_extra_args": [],
        "pi_environment": {},
        "max_session_idle_minutes": 30,
        "bot_status_message": "with pi | /help",
        "show_tool_notifications": True,
        "show_thinking": False,
    }

    with open(path, "w") as f:
        json.dump(default, f, indent=2)
    logger.info(f"Created default config at {path}")
    print(f"\n⚠️  Created default config at: {path}")
    print("   Please edit it with your Discord bot token and settings.\n")
