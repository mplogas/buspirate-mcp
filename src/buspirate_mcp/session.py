"""UART session management and raw logging."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ANSI_ESCAPE = re.compile(
    r"\x1b"           # ESC byte
    r"(?:"
    r"\[[0-9;]*"      # CSI sequence (possibly incomplete at chunk boundary)
    r"[a-zA-Z]?"      # optional final byte (may be in next chunk)
    r")"
)


def _clean_text(text: str) -> str:
    """Remove ANSI/VT100 escape sequences, stray ESC bytes, and null bytes."""
    text = _ANSI_ESCAPE.sub("", text)
    text = text.replace("\x1b", "")   # catch any remaining lone ESC bytes
    text = text.replace("\x00", "")
    return text


def _sanitize_name(name: str) -> str:
    """Strip everything except alphanumeric, hyphens, underscores."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


class Session:
    """A single active UART session with logging."""

    def __init__(
        self,
        session_id: str,
        engagement_path: Path,
        hardware: Any,
        baud: int,
        pins: dict[str, str],
    ) -> None:
        self.session_id = session_id
        self.engagement_path = engagement_path
        self.hardware = hardware
        self.baud = baud
        self.pins = pins
        self.connected = True

        log_path = engagement_path / "logs" / "uart-raw.log"
        self._log_file = open(log_path, "a", encoding="utf-8")

    def log_rx(self, data: bytes) -> None:
        """Log received data with timestamp. Strips ANSI escape codes."""
        if not self.connected:
            raise ValueError("Session is disconnected")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = _clean_text(data.decode("utf-8", errors="replace"))
        self._log_file.write(f"[{ts}] RX: {text}\n")
        self._log_file.flush()

    def log_tx(self, data: bytes) -> None:
        """Log transmitted data with timestamp. Strips ANSI escape codes."""
        if not self.connected:
            raise ValueError("Session is disconnected")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = _clean_text(data.decode("utf-8", errors="replace"))
        self._log_file.write(f"[{ts}] TX: {text}\n")
        self._log_file.flush()

    def close(self) -> None:
        """Close the log file and mark session as disconnected. Safe to call twice.

        Does NOT disconnect the hardware -- the hardware connection is
        shared across sessions and managed by the server. Only the log
        file is closed here.
        """
        if not self.connected:
            return
        self.connected = False
        self._log_file.close()
        self._log_file = None


class SessionManager:
    """Manages active UART sessions."""

    def __init__(self, engagements_dir: Path | str) -> None:
        self._engagements_dir = Path(engagements_dir)
        self._sessions: dict[str, Session] = {}

    def create(
        self,
        name: str,
        hardware: Any,
        baud: int,
        pins: dict[str, str],
        device_path: str = "",
    ) -> Session:
        """Create a new engagement session with logging directory."""
        sanitized = _sanitize_name(name)
        if not sanitized:
            sanitized = "unnamed"
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        folder_name = f"{date_prefix}-{sanitized}"

        engagement_path = self._engagements_dir / folder_name
        # If folder already exists (duplicate name same day), append session ID
        if engagement_path.exists():
            folder_name = f"{folder_name}-{str(uuid.uuid4())[:8]}"
            engagement_path = self._engagements_dir / folder_name
        (engagement_path / "logs").mkdir(parents=True, exist_ok=True)
        (engagement_path / "artifacts").mkdir(parents=True, exist_ok=True)

        session_id = str(uuid.uuid4())[:8]

        # Write engagement config
        config = {
            "session_id": session_id,
            "name": sanitized,
            "date": date_prefix,
            "device_path": device_path,
            "baud": baud,
            "pins": pins,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        config_path = engagement_path / "config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")

        session = Session(
            session_id=session_id,
            engagement_path=engagement_path,
            hardware=hardware,
            baud=baud,
            pins=pins,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        """Get an active session by ID. Raises KeyError if not found."""
        return self._sessions[session_id]

    def close(self, session_id: str) -> None:
        """Close and remove a session."""
        session = self._sessions.pop(session_id)
        session.close()
