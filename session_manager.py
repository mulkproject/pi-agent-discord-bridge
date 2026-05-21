"""
Session Manager — maps Discord channels/threads to pi RPC sessions.

Each Discord thread (or channel) gets its own pi RPC subprocess,
so conversations stay isolated with their own context windows.
"""

import logging
import time
import threading
from typing import Optional

from pi_rpc_client import PiRpcClient

logger = logging.getLogger("session_manager")


class PiSession:
    """Wrapper around a PiRpcClient with per-session metadata."""

    def __init__(self, session_id: str, pi_command: str = "pi", cwd: Optional[str] = None):
        self.session_id = session_id
        self.client = PiRpcClient(pi_command=pi_command, cwd=cwd)
        self.created_at = time.monotonic()
        self.last_used_at = time.monotonic()
        self.message_count = 0
        self._lock = threading.Lock()

    def start(self):
        self.client.start()

    def stop(self):
        self.client.stop()

    def is_expired(self, max_idle_seconds: float = 1800) -> bool:
        """Check if session has been idle too long (default 30 min)."""
        return (time.monotonic() - self.last_used_at) > max_idle_seconds

    def touch(self):
        """Mark session as recently used."""
        self.last_used_at = time.monotonic()
        self.message_count += 1


class SessionManager:
    """
    Manages a pool of pi sessions, one per Discord channel/thread.

    Automatically cleans up idle sessions.
    """

    def __init__(
        self,
        pi_command: str = "pi",
        max_idle_seconds: float = 1800,
        cleanup_interval: float = 60,
    ):
        self.pi_command = pi_command
        self.max_idle_seconds = max_idle_seconds
        self._sessions: dict[str, PiSession] = {}
        self._lock = threading.Lock()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="session-cleanup",
        )
        self._running = False

    def start(self):
        """Start the session manager and its cleanup thread."""
        self._running = True
        self._cleanup_thread.start()
        logger.info("Session manager started")

    def stop(self):
        """Stop all sessions and the cleanup thread."""
        self._running = False
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                logger.info(f"Stopping session: {session_id}")
                session.stop()
            self._sessions.clear()
        logger.info("Session manager stopped")

    def get_or_create(self, channel_id: str, cwd: Optional[str] = None) -> PiSession:
        """
        Get an existing session for a channel, or create a new one.
        
        Args:
            channel_id: Discord channel/thread ID
            cwd: Working directory for this session (None = bot's cwd)
        """
        with self._lock:
            if channel_id in self._sessions:
                session = self._sessions[channel_id]
                session.touch()
                return session

            logger.info(f"Creating new session for channel: {channel_id} (cwd={cwd})")
            session = PiSession(
                session_id=channel_id,
                pi_command=self.pi_command,
                cwd=cwd,
            )
            session.start()
            if cwd:
                logger.info(f"Pi subprocess will start in: {cwd}")
            session.touch()
            self._sessions[channel_id] = session
            return session

    def remove(self, channel_id: str):
        """Remove and stop a session."""
        with self._lock:
            session = self._sessions.pop(channel_id, None)
            if session:
                logger.info(f"Removing session: {channel_id}")
                session.stop()

    def get_stats(self) -> dict:
        """Get stats about active sessions."""
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "sessions": [
                    {
                        "id": sid,
                        "messages": s.message_count,
                        "idle_seconds": round(time.monotonic() - s.last_used_at, 1),
                    }
                    for sid, s in self._sessions.items()
                ],
            }

    def _cleanup_loop(self):
        """Background loop that removes idle sessions."""
        while self._running:
            time.sleep(self.max_idle_seconds / 2)  # Check at half the idle time
            self._cleanup_idle()

    def _cleanup_idle(self):
        """Remove sessions that have been idle too long."""
        with self._lock:
            idle_channels = [
                cid
                for cid, session in self._sessions.items()
                if session.is_expired(self.max_idle_seconds)
            ]
            sessions_to_stop = []
            for cid in idle_channels:
                logger.info(f"Cleaning up idle session: {cid}")
                session = self._sessions.pop(cid, None)
                if session:
                    sessions_to_stop.append(session)
        # Stop outside the lock (may block)
        for session in sessions_to_stop:
            session.stop()
