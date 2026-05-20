"""
Pi RPC Client — manages a `pi --mode rpc` subprocess.

Communicates with pi via JSONL over stdin/stdout.
Handles event streaming, tool output, and error recovery.
"""

import subprocess
import json
import threading
import queue
import time
import os
import signal
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger("pi_rpc")

# ──────────────────────────────────────────────
# Event types emitted by pi RPC mode
# ──────────────────────────────────────────────

@dataclass
class PiEvent:
    """Base class for parsed pi RPC events."""
    raw: dict

@dataclass
class MessageDeltaEvent(PiEvent):
    """Streaming text delta from the assistant."""
    delta: str
    full_text_so_far: str = ""

@dataclass
class ToolExecutionStartEvent(PiEvent):
    tool_name: str
    args: dict

@dataclass
class ToolExecutionUpdateEvent(PiEvent):
    tool_name: str
    partial_output: str

@dataclass
class ToolExecutionEndEvent(PiEvent):
    tool_name: str
    result_text: str
    is_error: bool

@dataclass
class AgentEndEvent(PiEvent):
    """Agent finished processing a prompt."""
    pass

@dataclass
class AgentStartEvent(PiEvent):
    """Agent started processing a prompt."""
    pass

@dataclass
class CompactionEvent(PiEvent):
    """Compaction event."""
    pass

@dataclass
class ErrorEvent(PiEvent):
    """Some error occurred."""
    error: str

@dataclass
class ResponseEvent(PiEvent):
    """Response to a command."""
    command: str
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None


class PiRpcClient:
    """
    Manages a `pi --mode rpc` subprocess.

    Usage:
        client = PiRpcClient()
        client.start()

        # Send a prompt (non-blocking)
        client.send_prompt("Hello, world!")

        # Or use the helper that streams events to a callback
        result = client.prompt_sync("Hello!", timeout=120)

        client.stop()
    """

    def __init__(
        self,
        pi_command: str = "pi",
        extra_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ):
        self.pi_command = pi_command
        self.extra_args = extra_args or []
        self.env = {**os.environ, **(env or {})}

        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._event_queue: queue.Queue = queue.Queue()
        self._running = False
        self._request_id_counter = 0
        self._pending_responses: dict[str, queue.Queue] = {}

    # ── Lifecycle ─────────────────────────────

    def start(self, timeout: float = 15.0):
        """Spawn pi in RPC mode."""
        if self._process is not None:
            logger.warning("PiRpcClient already running")
            return

        cmd = [self.pi_command, "--mode", "rpc", "--no-session"] + self.extra_args
        logger.info(f"Starting pi: {' '.join(cmd)}")

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
        )

        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="pi-rpc-reader"
        )
        self._reader_thread.start()

        # Wait for pi to be ready (first event or timeout)
        try:
            self._event_queue.get(timeout=timeout)
            logger.info("Pi RPC client started successfully")
        except queue.Empty:
            logger.warning("Pi started but no initial event received (continuing)")

    def stop(self, timeout: float = 5.0):
        """Gracefully shut down the pi subprocess."""
        self._running = False
        if self._process:
            logger.info("Stopping pi...")
            self._process.stdin.close()
            self._process.wait(timeout=timeout)
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait(timeout=2)
            self._process = None
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)
        logger.info("Pi RPC client stopped")

    def restart(self):
        """Restart the pi subprocess (useful after errors)."""
        self.stop()
        time.sleep(0.5)
        self.start()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ── Commands ───────────────────────────────

    def send_command(self, cmd: dict) -> str:
        """
        Send a JSON command to pi. Returns the request ID.
        """
        if not self.is_running:
            raise RuntimeError("Pi RPC client is not running")

        # Add optional id for correlation
        cmd = dict(cmd)
        if "id" not in cmd:
            self._request_id_counter += 1
            cmd["id"] = f"req-{self._request_id_counter}"

        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        logger.debug(f"→ {line.strip()}")
        self._process.stdin.write(line)
        self._process.stdin.flush()
        return cmd["id"]

    def send_prompt(
        self,
        message: str,
        streaming_behavior: Optional[str] = None,
        images: Optional[list[dict]] = None,
    ) -> str:
        """Send a user prompt. Returns request ID.

        Args:
            message: The prompt text.
            streaming_behavior: "steer" or "followUp" during active streams.
            images: Optional list of image dicts with format:
                   {"type": "image", "data": "base64...", "mimeType": "image/png"}
        """
        cmd = {"type": "prompt", "message": message}
        if streaming_behavior:
            cmd["streamingBehavior"] = streaming_behavior
        if images:
            cmd["images"] = images
        return self.send_command(cmd)

    def send_steer(self, message: str) -> str:
        """Queue a steering message during streaming."""
        return self.send_command({"type": "steer", "message": message})

    def send_follow_up(self, message: str) -> str:
        """Queue a follow-up message."""
        return self.send_command({"type": "follow_up", "message": message})

    def abort(self) -> str:
        """Abort the current agent operation."""
        return self.send_command({"type": "abort"})

    def set_model(self, provider: str, model_id: str) -> str:
        """Switch to a specific model. Returns request ID."""
        return self.send_command({
            "type": "set_model",
            "provider": provider,
            "modelId": model_id,
        })

    def get_available_models(self) -> list[dict]:
        """Get available models. Blocks until response."""
        req_id = self.send_command({"type": "get_available_models"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            event = self.poll_event(timeout=5)
            if isinstance(event, ResponseEvent) and event.command == "get_available_models":
                if event.success and event.data:
                    return event.data.get("models", [])
                break
        return []

    def get_state(self) -> Optional[dict]:
        """Get current session state."""
        req_id = self.send_command({"type": "get_state"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            event = self.poll_event(timeout=5)
            if isinstance(event, ResponseEvent) and event.command == "get_state":
                if event.success and event.data:
                    return event.data
                break
        return None

    # ── Event Reading ──────────────────────────

    def poll_event(self, timeout: Optional[float] = None) -> Optional[PiEvent]:
        """Get the next event, or None if timeout."""
        try:
            raw = self._event_queue.get(timeout=timeout)
            return self._parse_event(raw)
        except queue.Empty:
            return None

    def iter_events(self, timeout: Optional[float] = None):
        """Generator that yields parsed events."""
        while True:
            event = self.poll_event(timeout=timeout)
            if event is None:
                break
            yield event

    def prompt_sync(
        self,
        message: str,
        timeout: float = 300,
        on_delta: Optional[Callable[[str], None]] = None,
        on_tool: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
        images: Optional[list[dict]] = None,
    ) -> str:
        """
        Send a prompt and collect the full response synchronously.

        Args:
            message: The prompt text.
            timeout: Max seconds to wait.
            on_delta: Called with each text delta as it arrives.
            on_tool: Called when a tool starts (tool_name, args).
            on_tool_result: Called when a tool completes (tool_name, result_text).
            images: Optional list of image dicts for the RPC prompt.

        Returns:
            The complete assistant response text.
        """
        self.send_prompt(message, images=images)
        full_text = ""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            event = self.poll_event(timeout=min(remaining, 1.0))
            if event is None:
                continue

            if isinstance(event, MessageDeltaEvent):
                full_text += event.delta
                if on_delta:
                    on_delta(event.delta)
                logger.info(f"Text delta ({len(event.delta)}): {event.delta[:50]}")

            elif isinstance(event, ToolExecutionStartEvent):
                logger.info(f"Tool started: {event.tool_name}")
                if on_tool:
                    on_tool(event.tool_name, event.args)

            elif isinstance(event, ToolExecutionEndEvent):
                logger.info(f"Tool ended: {event.tool_name}")
                if on_tool_result:
                    on_tool_result(event.tool_name, event.result_text)

            elif isinstance(event, AgentEndEvent):
                logger.info(f"Agent finished. Collected {len(full_text)} chars")
                break

            elif isinstance(event, ErrorEvent):
                logger.error(f"Pi error: {event.error}")
                break

        return full_text

    # ── Internal ───────────────────────────────

    def _reader_loop(self):
        """Read JSONL lines from pi's stdout in a background thread."""
        try:
            for line in self._process.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._event_queue.put(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse pi output: {e} — line: {line[:200]}")
        except Exception as e:
            logger.error(f"Reader thread error: {e}")
        finally:
            self._running = False

    def _parse_event(self, raw: dict) -> PiEvent:
        """Convert raw JSON dict to a typed PiEvent."""
        event_type = raw.get("type", "")

        # ── Response to a command
        if event_type == "response":
            return ResponseEvent(
                raw=raw,
                command=raw.get("command", ""),
                success=raw.get("success", False),
                data=raw.get("data"),
                error=raw.get("error"),
            )

        # ── Streaming text
        if event_type == "message_update":
            msg_event = raw.get("assistantMessageEvent", {})
            delta_type = msg_event.get("type", "")
            delta = msg_event.get("delta", "")
            if delta_type == "text_delta":
                return MessageDeltaEvent(raw=raw, delta=delta)

        # ── Tool events
        if event_type == "tool_execution_start":
            return ToolExecutionStartEvent(
                raw=raw,
                tool_name=raw.get("toolName", "?"),
                args=raw.get("args", {}),
            )

        if event_type == "tool_execution_update":
            partial = raw.get("partialResult", {})
            content = partial.get("content", [])
            text = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
            return ToolExecutionUpdateEvent(
                raw=raw,
                tool_name=raw.get("toolName", "?"),
                partial_output=text,
            )

        if event_type == "tool_execution_end":
            result = raw.get("result", {})
            content = result.get("content", [])
            text = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
            return ToolExecutionEndEvent(
                raw=raw,
                tool_name=raw.get("toolName", "?"),
                result_text=text,
                is_error=raw.get("isError", False),
            )

        # ── Agent lifecycle
        if event_type == "agent_start":
            return AgentStartEvent(raw=raw)

        if event_type == "agent_end":
            return AgentEndEvent(raw=raw)

        if event_type in ("compaction_start", "compaction_end"):
            return CompactionEvent(raw=raw)

        if event_type == "extension_error":
            return ErrorEvent(raw=raw, error=raw.get("error", "Unknown error"))

        # Fallback: return generic event
        return PiEvent(raw=raw)
